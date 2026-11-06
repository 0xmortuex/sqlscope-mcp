"""The eight tools against real databases."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path

import pytest
from conftest import has_fts5, has_strict, tree_state

from sqlscope_mcp import server
from sqlscope_mcp.db import SqlScopeError, fmt_cell
from sqlscope_mcp.schema import check_constraints

# ---------------------------------------------------------------- db_overview


def test_overview_counts_and_kinds(shop: Path) -> None:
    out = server.db_overview(str(shop))
    assert "7 tables (0 virtual), 1 views, 3 indexes, 1 triggers" in out
    assert "Encoding: UTF-8, journal mode: delete" in out
    assert "| customers | table | 200 | 6 |" in out
    assert "| orders | table | 302 | 4 | 1 FK; 1 trigger(s) |" in out
    assert "table WITHOUT ROWID" in out
    assert "| big_orders | view | (view) | 2 |" in out
    assert "müşteriler" in out and 'we"ird' in out


def test_overview_uses_stat1_when_analyzed(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    c.execute("CREATE INDEX i ON t(a)")
    c.executemany("INSERT INTO t VALUES (?, ?)", [(i, "x") for i in range(300)])
    c.execute("ANALYZE")
    c.commit()
    c.close()
    out = server.db_overview(str(db))
    assert "~300 (stat1)" in out and "estimates from the last ANALYZE" in out


def test_overview_row_count_cap(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (a)")
    c.execute("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r WHERE n < 150000) INSERT INTO t SELECT n FROM r")
    c.commit()
    c.close()
    assert ">=100,000" in server.db_overview(str(db))


def test_user_version_and_application_id(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    c = sqlite3.connect(db)
    c.execute("PRAGMA user_version = 42")
    c.execute("PRAGMA application_id = 1234")
    c.execute("CREATE TABLE t (a)")
    c.commit()
    c.close()
    out = server.db_overview(str(db))
    assert "user_version: 42" in out and "application_id: 1234" in out


def test_utf16_database(tmp_path: Path) -> None:
    db = tmp_path / "u16.db"
    c = sqlite3.connect(db)
    c.execute("PRAGMA encoding = 'UTF-16le'")
    c.execute("CREATE TABLE t (a TEXT)")
    c.execute("INSERT INTO t VALUES ('Çağrı')")
    c.commit()
    c.close()
    assert "UTF-16 little-endian" in server.db_overview(str(db))
    assert "Çağrı" in server.query(str(db), "SELECT a FROM t")


def test_empty_database_with_valid_header(tmp_path: Path) -> None:
    db = tmp_path / "e.db"
    c = sqlite3.connect(db)
    c.execute("PRAGMA user_version = 1")  # forces the file to be written
    c.commit()
    c.close()
    assert "no tables or views" in server.db_overview(str(db))


@pytest.mark.skipif(not has_fts5(), reason="this SQLite build has no FTS5")
def test_fts5_is_listed_and_does_not_break_anything(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    c = sqlite3.connect(db)
    c.execute("CREATE VIRTUAL TABLE docs USING fts5(title, body)")
    c.executemany("INSERT INTO docs VALUES (?, ?)", [(f"t{i}", f"hello world {i} needle") for i in range(50)])
    c.execute("CREATE TABLE plain (a TEXT)")
    c.execute("INSERT INTO plain VALUES ('needle in a plain table')")
    c.commit()
    c.close()
    before = tree_state(tmp_path)
    out = server.db_overview(str(db))
    assert "virtual (fts5)" in out and "Shadow tables" in out and "docs_data" in out
    assert "| docs_data |" not in out
    d = server.describe_table(str(db), "docs")
    assert "virtual table using fts5" in d and "title" in d
    assert "docs_content" in server.describe_table(str(db), "docs_content")  # still describable
    found = server.find_in_db(str(db), "needle")
    assert "plain" in found and "Not searched: docs (virtual)" in found
    assert "docs" in server.schema_docs(str(db))
    server.data_quality(str(db))
    server.schema_graph(str(db))
    assert "50" in server.query(str(db), "SELECT count(*) FROM docs WHERE docs MATCH 'needle'")
    assert tree_state(tmp_path) == before


def test_virtual_table_with_missing_module_does_not_break(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (a)")
    c.execute("INSERT INTO t VALUES (1)")
    c.execute("PRAGMA writable_schema = ON")
    c.execute("INSERT INTO sqlite_master VALUES ('table','ghost','ghost',0,'CREATE VIRTUAL TABLE ghost USING nosuchmodule(a)')")
    c.commit()
    c.close()
    out = server.db_overview(str(db))
    assert "virtual (nosuchmodule)" in out and "| t | table | 1 | 1 |" in out
    assert "no such module" in server.describe_table(str(db), "ghost")
    server.schema_docs(str(db))
    server.find_in_db(str(db), "1")
    server.data_quality(str(db))
    server.schema_graph(str(db))


@pytest.mark.skipif(not has_strict(), reason="STRICT tables need SQLite 3.37")
def test_strict_table(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (a INTEGER, b TEXT) STRICT")
    c.commit()
    c.close()
    assert "table STRICT" in server.db_overview(str(db))
    assert "STRICT" in server.describe_table(str(db), "t")


# ---------------------------------------------------------------- describe_table


def test_describe_table_details(shop: Path) -> None:
    out = server.describe_table(str(shop), "products")
    assert "| sku | TEXT | PK |" in out
    assert "| qty | INTEGER | NOT NULL | 0 |" in out
    assert "generated virtual" in out and "AS (price * 1.2)" in out
    assert "CHECK (price >= 0 AND price < 1000000)" in out and "CHECK (qty >= 0)" in out
    assert "<BLOB 1032 bytes 0x89504e47" in out  # size + hex prefix, never the bytes
    assert "\\x89" not in out and "PNG" not in out.split("Definition:")[1].split("Sample rows")[1]
    assert "Referenced by:\n  order_items(sku)" in out
    assert "blob,null" in out  # typeof() distribution


def test_describe_table_foreign_keys_and_triggers(shop: Path) -> None:
    out = server.describe_table(str(shop), "orders")
    assert "(customer_id) -> customers(id)  [ON DELETE CASCADE]" in out
    assert "trg_audit" in out and "idx_orders_placed" in out
    assert "alias of the rowid" in out
    assert "integer,text" in out  # mixed types in total


def test_describe_table_without_rowid_and_composite_pk(shop: Path) -> None:
    out = server.describe_table(str(shop), "order_items")
    assert "WITHOUT ROWID" in out and "Primary key: (order_id, sku)" in out
    assert "order_items" in out and "(order_id) -> orders(id)" in out


def test_describe_table_huge_text_is_truncated_with_length(shop: Path) -> None:
    out = server.describe_table(str(shop), "customers", sample_rows=1)
    assert "[5000 chars total]" in out
    assert "L" * 300 not in out


def test_describe_table_unicode_and_quoted_names(shop: Path) -> None:
    out = server.describe_table(str(shop), "müşteriler")
    assert "şehir" in out and "ad soyad" in out and "Çağrı Şahin" in out
    out = server.describe_table(str(shop), 'we"ird')
    assert 'co"l' in out and "select" in out and 'he said \\"hi\\"' not in out
    assert server.describe_table(str(shop), "CUSTOMERS").startswith("Table customers")


def test_describe_view_and_unknown_table(shop: Path) -> None:
    assert server.describe_table(str(shop), "big_orders").startswith("View big_orders")
    with pytest.raises(SqlScopeError, match="Did you mean: customers"):
        server.describe_table(str(shop), "customer")
    with pytest.raises(SqlScopeError, match="Available:"):
        server.describe_table(str(shop), "zzz")


def test_describe_table_sampling_note(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
    c.executemany("INSERT INTO t VALUES (?, ?)", [(i, i % 10) for i in range(1, 5001)])
    c.commit()
    c.close()
    out = server.describe_table(str(db), "t", stats_sample=500)
    assert "a sample of about 500 rows" in out
    assert "| v | INTEGER |" in out and "| 10 |" in out.replace("| 0% (0) | 10 |", "| 10 |")


def test_check_constraint_parser() -> None:
    sql = "CREATE TABLE t (a INT CHECK (a > 0), b TEXT CHECK (b IN ('x)', 'y(')), c INT, CONSTRAINT k CHECK (c < (1+2)), 'check (z)' INT)"
    assert check_constraints(sql) == ["a > 0", "b IN ('x)', 'y(')", "c < (1+2)"]
    assert check_constraints("CREATE TABLE t (a INT, mycheck(1))") == []


def test_fmt_cell() -> None:
    assert fmt_cell(None) == "NULL"
    assert fmt_cell(b"\x00\x01") == "<BLOB 2 bytes 0x0001>"
    assert fmt_cell(b"x" * 100).startswith("<BLOB 100 bytes 0x7878") and fmt_cell(b"x" * 100).endswith("...>")
    assert fmt_cell("a" * 10, 5) == "aaaaa... [10 chars total]"
    assert fmt_cell(1.5) == "1.5" and fmt_cell(7) == "7"


def test_invalid_utf8_text_does_not_crash(tmp_path: Path) -> None:
    db = tmp_path / "bad.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (a TEXT)")
    c.execute("INSERT INTO t VALUES (CAST(x'61ff62' AS TEXT))")
    c.commit()
    c.close()
    assert "a�b" in server.query(str(db), "SELECT a FROM t")


# ---------------------------------------------------------------- schema_graph


def test_schema_graph_text(shop: Path) -> None:
    out = server.schema_graph(str(shop))
    assert "orders (customer_id) -> customers(id)  [ON DELETE CASCADE]" in out
    assert "Tables with no declared relationships: audit" in out
    assert "Load order (parents first):" in out
    assert 'orders(customer_id) has no index. Suggestion: CREATE INDEX "idx_orders_customer_id" ON "orders" ("customer_id");' in out
    assert 'order_items(sku) has no index' in out
    assert "order_items(order_id) " not in out.split("Missing indexes")[1].split("Orphan")[0]  # covered by the PK
    assert "orders(customer_id) -> customers: 2 of 302" in out and "99999" in out
    assert "order_items(sku) -> products: 1 of 4" in out and "ZZZ" in out
    assert "order_items(order_id) -> orders: ok" in out


def test_schema_graph_mermaid(shop: Path) -> None:
    out = server.schema_graph(str(shop), "mermaid")
    assert out.startswith("```mermaid\nerDiagram")
    assert '"customers" |o--o{ "orders" : "customer_id"' in out
    assert '"orders" ||--o{ "order_items" : "order_id"' in out


def test_schema_graph_cycle_and_self_reference_and_composite(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    c = sqlite3.connect(db)
    c.executescript(
        """
        CREATE TABLE a (id INTEGER PRIMARY KEY, b_id INTEGER REFERENCES b(id));
        CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES a(id));
        CREATE TABLE emp (id INTEGER PRIMARY KEY, boss INTEGER REFERENCES emp(id));
        CREATE TABLE p (x INTEGER, y INTEGER, PRIMARY KEY (x, y));
        CREATE TABLE ch (id INTEGER PRIMARY KEY, x INTEGER, y INTEGER, FOREIGN KEY (x, y) REFERENCES p(x, y));
        CREATE INDEX ch_xy ON ch(y, x);
        CREATE TABLE lost (id INTEGER PRIMARY KEY, g INTEGER REFERENCES gone(id));
        INSERT INTO emp VALUES (1, NULL), (2, 1), (3, 77);
        INSERT INTO p VALUES (1, 1);
        INSERT INTO ch VALUES (1, 1, 1), (2, 1, 2), (3, NULL, 5);
        """
    )
    c.commit()
    c.close()
    out = server.schema_graph(str(db))
    assert "Circular references among: a, b" in out
    assert "emp(boss) -> emp: 1 of 2" in out and "77" in out
    assert "ch(x, y) -> p: 1 of 2" in out  # composite FK, the all-NULL-ish row is skipped
    assert "ch(x, y) has no index" not in out  # (y, x) covers {x, y}
    assert "lost(g) -> gone: parent table gone does not exist" in out


# ---------------------------------------------------------------- query


def test_query_markdown_csv_json(shop: Path) -> None:
    sql = "SELECT sku, price, thumb, NULL AS n FROM products ORDER BY sku"
    md = server.query(str(shop), sql)
    assert md.splitlines()[0] == "| sku | price | thumb | n |"
    assert "| A1 | 9.5 | <BLOB 1032 bytes" in md and "3 row(s), 4 column(s)" in md
    cs = server.query(str(shop), sql, format="csv")
    rows = list(csv.reader(io.StringIO(cs.split("\n\n")[0])))
    assert rows[0] == ["sku", "price", "thumb", "n"] and rows[2][0] == "B2" and rows[2][3] == "NULL"
    js = server.query(str(shop), sql, format="json")
    data = json.loads(js[: js.rindex("]") + 1])
    assert data[0]["sku"] == "A1" and data[0]["price"] == 9.5 and data[0]["thumb"].startswith("<BLOB 1032 bytes")
    assert data[0]["n"] is None


def test_query_truncation_and_validation(shop: Path) -> None:
    out = server.query(str(shop), "SELECT * FROM orders", max_rows=10)
    assert "10 row(s)" in out and "TRUNCATED" in out
    out = server.query(str(shop), "SELECT * FROM orders LIMIT 5", max_rows=10)
    assert "TRUNCATED" not in out
    with pytest.raises(ValueError, match="max_rows"):
        server.query(str(shop), "SELECT 1", max_rows=0)
    with pytest.raises(ValueError, match="max_rows"):
        server.query(str(shop), "SELECT 1", max_rows=10**6)
    with pytest.raises(SqlScopeError, match="no such table"):
        server.query(str(shop), "SELECT * FROM nope")
    with pytest.raises(SqlScopeError, match="syntax error"):
        server.query(str(shop), "SELECT FROM WHERE")


def test_query_byte_cap(tmp_path: Path) -> None:
    db = tmp_path / "wide.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (a TEXT)")
    c.executemany("INSERT INTO t VALUES (?)", [("x" * 400,) for _ in range(2000)])
    c.commit()
    c.close()
    out = server.query(str(db), "SELECT a FROM t", max_rows=2000)
    assert "TRUNCATED" in out and "KB cap" in out
    assert len(out) < 260_000


def test_query_with_cte_values_and_pragma_functions(shop: Path) -> None:
    assert "| 3 |" in server.query(str(shop), "WITH x(n) AS (VALUES (1),(2)) SELECT sum(n) FROM x")
    assert "| 1 |" in server.query(str(shop), "VALUES (1)")
    assert "customers" in server.query(str(shop), "SELECT name FROM pragma_table_list WHERE schema='main'")
    assert "email" in server.query(str(shop), "SELECT name FROM pragma_table_info('customers')")


def test_query_markdown_escapes_pipes_and_newlines(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (a)")
    c.execute("INSERT INTO t VALUES ('x|y\nz')")
    c.commit()
    c.close()
    out = server.query(str(db), "SELECT a FROM t")
    assert "x\\|y\\nz" in out and len(out.splitlines()[2:]) >= 1


# ---------------------------------------------------------------- explain_query


def test_explain_flags_full_scan_and_suggests_index(shop: Path) -> None:
    out = server.explain_query(str(shop), "SELECT * FROM orders WHERE customer_id = 5")
    assert "FULL TABLE SCAN of orders (302 rows)" in out
    assert 'CREATE INDEX "idx_orders_customer_id" ON "orders" ("customer_id");' in out
    assert "nothing was created" in out


def test_explain_search_with_index_is_clean(shop: Path) -> None:
    out = server.explain_query(str(shop), "SELECT * FROM orders WHERE placed = '2024-04-01'")
    assert "idx_orders_placed" in out and "no full table scans" in out


def test_explain_temp_btree_and_alias(shop: Path) -> None:
    out = server.explain_query(str(shop), "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id ORDER BY o.total")
    assert "FULL TABLE SCAN of orders" in out and "TEMP B-TREE for ORDER BY" in out


def test_explain_automatic_index(shop: Path) -> None:
    out = server.explain_query(
        str(shop), "SELECT * FROM (SELECT * FROM customers) s JOIN orders o ON o.customer_id = s.id + 0 JOIN order_items i ON i.sku = o.placed"
    )
    # whichever automatic index the planner picks, it must be called out with a CREATE INDEX text
    if "AUTOMATIC" in out:
        assert "AUTOMATIC INDEX" in out and "Create it once: CREATE INDEX" in out


def test_explain_automatic_index_deterministic(tmp_path: Path) -> None:
    db = tmp_path / "ai.db"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE a (x); CREATE TABLE b (y);")
    c.executemany("INSERT INTO a VALUES (?)", [(i,) for i in range(100)])
    c.executemany("INSERT INTO b VALUES (?)", [(i,) for i in range(100)])
    c.commit()
    c.close()
    out = server.explain_query(str(db), "SELECT * FROM a JOIN b ON b.y = a.x")
    assert "AUTOMATIC INDEX on b(y)" in out or "AUTOMATIC INDEX on a(x)" in out
    assert "Create it once: CREATE INDEX" in out


def test_explain_rejects_writes_and_never_runs_the_query(shop: Path) -> None:
    with pytest.raises(SqlScopeError):
        server.explain_query(str(shop), "DELETE FROM orders")
    with pytest.raises(SqlScopeError):
        server.explain_query(str(shop), "SELECT 1; DELETE FROM orders")
    out = server.explain_query(str(shop), "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r) SELECT count(*) FROM r")
    assert "was not run" in out


# ---------------------------------------------------------------- find_in_db


def test_find_in_db_across_tables(shop: Path) -> None:
    out = server.find_in_db(str(shop), "user5@")
    assert "customers" in out and "email = 'user5@example.com'" in out
    out = server.find_in_db(str(shop), "ZZZ")
    assert "order_items" in out and "pk=(3, ZZZ)" in out


def test_find_in_db_numbers_case_and_turkish(shop: Path) -> None:
    assert "1001" in server.find_in_db(str(shop), "1001")
    assert "No matches" in server.find_in_db(str(shop), "ISTANBUL", case_sensitive=True)
    ci = server.find_in_db(str(shop), "ISTANBUL")
    assert "customers" in ci and "müşteriler" in ci
    # SQLite folds only ASCII: lowercase 'istanbul' matches 'istanbul' (rowid 3) but not the dotted capital 'Istanbul'
    only = server.find_in_db(str(shop), "istanbul", max_matches_per_table=50).split("müşteriler:")[1]
    assert "rowid=3:" in only and "rowid=1:" not in only
    tr = server.find_in_db(str(shop), "Çağrı")
    assert "ad soyad = 'Çağrı Şahin'" in tr


def test_find_in_db_caps_and_wildcard_escaping(shop: Path) -> None:
    out = server.find_in_db(str(shop), "example", max_matches_per_table=2)
    assert "customers: 2+ matching row(s)" in out
    assert "No matches" in server.find_in_db(str(shop), "%")  # a literal percent sign, not a wildcard
    assert "No matches" in server.find_in_db(str(shop), "user_1@")  # underscore is literal too
    with pytest.raises(SqlScopeError, match="empty"):
        server.find_in_db(str(shop), "")


def test_find_in_db_does_not_dump_blobs_and_blob_text_is_skipped(shop: Path) -> None:
    out = server.find_in_db(str(shop), "PNG")
    assert "products" not in out


def test_find_in_db_time_budget(tmp_path: Path) -> None:
    db = tmp_path / "slow.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (a TEXT)")
    c.execute("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r WHERE n < 400000) INSERT INTO t SELECT 'abcdefghij' || n FROM r")
    c.commit()
    c.close()
    out = server.find_in_db(str(db), "zzz-not-there", seconds_per_table=0.1)
    # a 0.1 s budget over 400k rows either finishes (fast machine) or reports the budget
    assert "No matches" in out


# ---------------------------------------------------------------- data_quality


def test_data_quality_findings(shop: Path) -> None:
    out = server.data_quality(str(shop))
    assert "orders(customer_id) -> customers: 2 of 302" in out
    assert "notes: 99% NULL" in out
    assert "email looks like a key but is not unique: 1 value(s) repeat" in out and "user5@example.com" in out
    assert "email: 2 value(s) collide after lower() and trim()" in out
    assert "total (declared INTEGER): mixed storage types: integer 301, text 1" in out and "'n/a'" in out
    assert "created: date-looking text" in out and "4 different formats" in out and "impossible month or day" in out
    assert "placed: date-looking text, one consistent format" in out
    assert "no PRIMARY KEY or UNIQUE constraint" in out


def test_data_quality_single_table_and_errors(shop: Path) -> None:
    out = server.data_quality(str(shop), table="orders")
    assert "## orders" in out and "## customers" not in out
    with pytest.raises(SqlScopeError, match="No table"):
        server.data_quality(str(shop), table="nope")
    with pytest.raises(ValueError, match="sample"):
        server.data_quality(str(shop), sample=1)


def test_data_quality_null_vs_empty_and_always_null(tmp_path: Path) -> None:
    db = tmp_path / "n.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, dead TEXT, mix NUMERIC)")
    c.executemany("INSERT INTO t VALUES (?, ?, NULL, ?)", [(i, "" if i % 2 else None, i if i % 3 else i + 0.5) for i in range(1, 41)])
    c.commit()
    c.close()
    out = server.data_quality(str(db))
    assert "dead: always NULL" in out
    assert "a: both NULL" in out and "empty string" in out
    assert "integers and reals mixed" in out


def test_data_quality_rows_identical_apart_from_surrogate_key(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, city TEXT)")
    c.executemany("INSERT INTO t(name, city) VALUES (?, ?)", [("a", "x"), ("a", "x"), ("a", "x"), ("b", "y")])
    c.execute("CREATE TABLE nat (a INTEGER, b INTEGER, qty INTEGER, PRIMARY KEY (a, b))")
    c.executemany("INSERT INTO nat VALUES (?, ?, 1)", [(1, 1), (1, 2), (2, 1)])
    c.commit()
    c.close()
    out = server.data_quality(str(db))
    assert "1 group(s) of rows are identical in every non-key column (2 redundant rows)" in out
    assert out.count("identical in every non-key column") == 1  # a natural composite key does not trigger it


def test_data_quality_clean_database(tmp_path: Path) -> None:
    db = tmp_path / "ok.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    c.executemany("INSERT INTO t(name) VALUES (?)", [(f"n{i}",) for i in range(30)])
    c.commit()
    c.close()
    out = server.data_quality(str(db))
    assert "0 warning(s)" in out


# ---------------------------------------------------------------- schema_docs


def test_schema_docs_markdown(shop: Path) -> None:
    out = server.schema_docs(str(shop))
    assert out.startswith("# Database schema: shop.db")
    assert "```mermaid\nerDiagram" in out
    assert "### orders" in out and "references customers(id)" in out
    assert "### müşteriler" in out and "| şehir | TEXT |" in out
    assert "Checks: `price >= 0 AND price < 1000000`; `qty >= 0`" in out
    assert "## Views" in out and "### big_orders" in out
    assert "## Triggers" in out and "### trg_audit (on orders)" in out
    assert "Table (WITHOUT ROWID)" in out
