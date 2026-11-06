"""Attack tests: nothing a client sends may change the database file, and nothing may be created next to it."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest
from conftest import build_shop, tree_state

from sqlscope_mcp import server
from sqlscope_mcp.db import SqlScopeError, connect, open_db

ATTACKS = [
    "INSERT INTO customers(id, email) VALUES (9999, 'x')",
    "INSERT OR REPLACE INTO products(sku, title) VALUES ('A1', 'hacked')",
    "REPLACE INTO products(sku, title) VALUES ('A1', 'hacked')",
    "UPDATE customers SET email = 'x'",
    "DELETE FROM customers",
    "DROP TABLE customers",
    "DROP VIEW big_orders",
    "DROP INDEX idx_orders_placed",
    "CREATE TABLE evil (a)",
    "CREATE TEMP TABLE evil (a)",
    "CREATE TEMPORARY VIEW evilv AS SELECT 1",
    "CREATE TABLE evil2 AS SELECT * FROM customers",
    "CREATE INDEX evil_ix ON customers(email)",
    "CREATE VIEW evilv AS SELECT 1",
    "CREATE TRIGGER evil_t AFTER INSERT ON audit BEGIN SELECT 1; END",
    "CREATE VIRTUAL TABLE evilf USING fts5(a)",
    "ALTER TABLE customers ADD COLUMN evil TEXT",
    "ALTER TABLE customers RENAME TO evil",
    "ATTACH DATABASE ':memory:' AS m",
    "ATTACH DATABASE 'attacker.db' AS m",
    "ATTACH 'file:attacker.db?mode=rwc' AS m",
    "DETACH DATABASE main",
    "VACUUM",
    "VACUUM INTO 'copy.db'",
    "VACUUM main INTO 'copy2.db'",
    "PRAGMA writable_schema = ON",
    "PRAGMA writable_schema=1",
    "PRAGMA query_only = OFF",
    "PRAGMA journal_mode = DELETE",
    "PRAGMA journal_mode = WAL",
    "PRAGMA user_version = 99",
    "PRAGMA application_id = 5",
    "PRAGMA foreign_keys = ON",
    "PRAGMA wal_checkpoint(TRUNCATE)",
    "PRAGMA incremental_vacuum",
    "PRAGMA optimize",
    "PRAGMA integrity_check",
    "PRAGMA temp_store = FILE",
    "PRAGMA trusted_schema = ON",
    "SELECT * FROM pragma_journal_mode('delete')",
    "SELECT * FROM pragma_user_version('7')",
    "REINDEX",
    "ANALYZE",
    "BEGIN",
    "BEGIN IMMEDIATE",
    "SAVEPOINT s",
    "COMMIT",
    "WITH x AS (SELECT 1) INSERT INTO audit SELECT 'a', 'b' FROM x",
    "WITH x AS (SELECT 1) DELETE FROM audit",
    "SELECT load_extension('nonexistent')",
    "SELECT fts3_tokenizer('simple')",
    "INSERT INTO sqlite_master VALUES ('table','x','x',1,'CREATE TABLE x(a)')",
    "UPDATE sqlite_master SET sql = 'x'",
]


def run_all(con: sqlite3.Connection) -> list[str]:
    """Execute every attack; return those that did NOT raise."""
    survived = []
    for sql in ATTACKS:
        try:
            con.execute(sql).fetchall()
        except (sqlite3.Error, sqlite3.Warning):
            continue
        survived.append(sql)
    return survived


def test_authorizer_layer_denies_every_attack(shop: Path) -> None:
    before = tree_state(shop.parent)
    g = open_db(shop)
    try:
        assert run_all(g.con) == []
        # the authorizer, not the keyword gate, said no: the error is surfaced as a clear denial
        with pytest.raises(SqlScopeError, match="Denied: INSERT"):
            g.fetch("INSERT INTO audit VALUES ('a','b')")
        with pytest.raises(SqlScopeError, match="Denied: ATTACH"):
            g.fetch("ATTACH ':memory:' AS m")
        with pytest.raises(SqlScopeError, match="Denied: PRAGMA writable_schema"):
            g.fetch("PRAGMA writable_schema = ON")
        with pytest.raises(SqlScopeError, match="Denied: VACUUM|Denied: ATTACH"):
            g.fetch("VACUUM INTO 'copy.db'")
        with pytest.raises(SqlScopeError, match="Denied: function load_extension"):
            g.fetch("SELECT load_extension('x')")
    finally:
        g.close()
    assert tree_state(shop.parent) == before, "files changed or were created"


def test_readonly_uri_alone_blocks_writes(shop: Path) -> None:
    """Independent layer: even without the authorizer the connection is read-only."""
    before = tree_state(shop.parent)
    con = sqlite3.connect(shop.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    for sql in [
        "INSERT INTO audit VALUES ('a','b')",
        "UPDATE customers SET email='x'",
        "DELETE FROM audit",
        "CREATE TABLE evil (a)",
        "DROP TABLE audit",
        "VACUUM",
    ]:
        with pytest.raises(sqlite3.Error):
            con.execute(sql)
    # NB: VACUUM INTO is NOT stopped by mode=ro (it writes a new file elsewhere); the authorizer
    # layer denies it, see test_authorizer_layer_denies_every_attack.
    with pytest.raises(sqlite3.Error):
        con.executescript("PRAGMA writable_schema=ON; DROP TABLE audit;")
    con.close()
    assert tree_state(shop.parent) == before


def test_query_only_pragma_alone_blocks_writes(tmp_path: Path) -> None:
    """Independent layer: PRAGMA query_only refuses writes even on a read-write connection."""
    copy = tmp_path / "copy.db"
    build_shop(copy)
    before = tree_state(tmp_path)
    con = sqlite3.connect(copy, isolation_level=None)
    con.execute("PRAGMA query_only = ON")
    for sql in [
        "INSERT INTO audit VALUES ('a','b')",
        "CREATE TABLE evil (a)",
        "DROP TABLE audit",
        "UPDATE customers SET email='x'",
    ]:
        with pytest.raises(sqlite3.Error):
            con.execute(sql)
    con.close()
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO audit VALUES ('a','b')",
        "DROP TABLE customers",
        "ATTACH ':memory:' AS m",
        "PRAGMA writable_schema = ON",
        "CREATE TEMP TABLE t (a)",
        "VACUUM INTO 'x.db'",
        "PRAGMA table_info(customers)",
        "EXPLAIN SELECT 1",
        "SELECT 1; DROP TABLE customers",
        "  /* SELECT */ DELETE FROM customers",
        "-- SELECT\nDROP TABLE customers",
        "",
        "   ",
    ],
)
def test_query_tool_rejects_non_select(shop: Path, sql: str) -> None:
    before = tree_state(shop.parent)
    with pytest.raises(SqlScopeError):
        server.query(str(shop), sql)
    assert tree_state(shop.parent) == before


def test_stacked_statement_behind_a_select_is_refused(shop: Path) -> None:
    before = tree_state(shop.parent)
    with pytest.raises(SqlScopeError, match="one statement"):
        server.query(str(shop), "SELECT 1; DROP TABLE customers")
    with pytest.raises(SqlScopeError):
        server.query(str(shop), "SELECT 1; ATTACH ':memory:' AS m")
    assert tree_state(shop.parent) == before
    out = server.query(str(shop), "SELECT count(*) FROM orders;")  # a trailing semicolon is fine
    assert "302" in out


def test_select_with_side_effects_is_still_denied(shop: Path) -> None:
    for sql in ["SELECT * FROM pragma_writable_schema('on')", "WITH x AS (SELECT 1) SELECT load_extension('a')"]:
        with pytest.raises(SqlScopeError):
            server.query(str(shop), sql)


def test_tools_leave_no_trace(shop: Path) -> None:
    before = tree_state(shop.parent)
    p = str(shop)
    server.db_overview(p)
    server.describe_table(p, "customers")
    server.describe_table(p, "orders")
    server.schema_graph(p)
    server.schema_graph(p, "mermaid")
    server.query(p, "SELECT * FROM customers LIMIT 3")
    server.explain_query(p, "SELECT * FROM orders WHERE customer_id = 3")
    server.find_in_db(p, "Istanbul")
    server.data_quality(p)
    server.schema_docs(p)
    assert tree_state(shop.parent) == before
    assert not any(n.endswith(("-journal", "-wal", "-shm")) for n in os.listdir(shop.parent))


def test_runaway_query_is_interrupted(shop: Path) -> None:
    t = time.monotonic()
    with pytest.raises(SqlScopeError, match="interrupted"):
        server.query(
            str(shop),
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r) SELECT count(*) FROM r",
            timeout_seconds=0.5,
        )
    assert time.monotonic() - t < 20


def test_cross_join_explosion_is_interrupted(shop: Path) -> None:
    with pytest.raises(SqlScopeError, match="interrupted"):
        server.query(str(shop), "SELECT count(*) FROM orders a, orders b, orders c, orders d, orders e", timeout_seconds=0.5)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT zeroblob(2000000000)",
        "SELECT length(hex(zeroblob(100000000)))",
        "SELECT length(randomblob(100000000))",
        "SELECT length(replace(hex(zeroblob(30000000)), '0', 'BBBB'))",
    ],
)
def test_huge_values_are_refused(shop: Path, sql: str) -> None:
    with pytest.raises(SqlScopeError, match="too big|larger than"):
        server.query(str(shop), sql, timeout_seconds=20)


def test_sql_length_limit(shop: Path) -> None:
    with pytest.raises(SqlScopeError):
        server.query(str(shop), "SELECT 1 WHERE 1 IN (" + ",".join(["1"] * 600_000) + ")")


# ---------------------------------------------------------------- WAL


def _wal_db(path: Path) -> None:
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    c.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"row {i}") for i in range(500)])
    c.commit()
    c.close()  # last connection closed: SQLite removes -wal and -shm


def test_closed_wal_database_creates_no_files(tmp_path: Path) -> None:
    db = tmp_path / "w.db"
    _wal_db(db)
    assert sorted(os.listdir(tmp_path)) == ["w.db"]
    before = tree_state(tmp_path)
    out = server.db_overview(str(db))
    assert "journal mode: wal" in out and "500" in out
    assert "no pending -wal file" in out
    server.query(str(db), "SELECT count(*) FROM t")
    server.describe_table(str(db), "t")
    server.data_quality(str(db))
    assert sorted(os.listdir(tmp_path)) == ["w.db"]
    assert tree_state(tmp_path) == before


def test_wal_with_pending_changes_is_read_correctly_and_untouched(tmp_path: Path) -> None:
    db = tmp_path / "w.db"
    _wal_db(db)
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute("INSERT INTO t VALUES (1000, 'only in the wal')")
    assert (tmp_path / "w.db-wal").stat().st_size > 0

    def state() -> dict[str, str]:
        # -shm is SQLite's lock/index scratch file for the WAL, not data
        return {k: v for k, v in tree_state(tmp_path).items() if not k.endswith("-shm")}

    before = state()
    out = server.query(str(db), "SELECT count(*) AS n FROM t")
    assert "501" in out, out  # the row that exists only in the WAL is visible
    assert "WAL:" in server.db_overview(str(db))
    assert "only in the wal" in server.find_in_db(str(db), "only in the wal")
    assert state() == before
    # and the writer is unharmed
    writer.execute("INSERT INTO t VALUES (1001, 'later')")
    writer.close()
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM t").fetchone() == (502,)
    c.close()


def test_rollback_journal_database_creates_no_files(tmp_path: Path) -> None:
    db = tmp_path / "j.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (a)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()
    before = tree_state(tmp_path)
    server.db_overview(str(db))
    assert tree_state(tmp_path) == before


# ---------------------------------------------------------------- sandbox


def test_sandbox_blocks_escapes(tmp_path: Path) -> None:
    box = tmp_path / "box"
    sibling = tmp_path / "box2"
    box.mkdir()
    sibling.mkdir()
    build_shop(box / "in.db")
    build_shop(sibling / "out.db")
    build_shop(tmp_path / "outside.db")
    server.configure(str(box))
    assert "customers" in server.db_overview("in.db")
    assert "customers" in server.db_overview(str(box / "in.db"))
    for bad in [
        "../outside.db",
        str(tmp_path / "outside.db"),
        "../box2/out.db",
        str(sibling / "out.db"),
        "sub/../../outside.db",
        "..\\outside.db",
    ]:
        with pytest.raises(SqlScopeError):
            server.db_overview(bad)


def test_sandbox_blocks_symlink_escape(tmp_path: Path) -> None:
    box = tmp_path / "box"
    box.mkdir()
    build_shop(tmp_path / "outside.db")
    try:
        (box / "link.db").symlink_to(tmp_path / "outside.db")
    except OSError:
        pytest.skip("cannot create symlinks on this machine (needs privileges on Windows)")
    server.configure(str(box))
    with pytest.raises(SqlScopeError, match="outside the allowed folder"):
        server.db_overview("link.db")


def test_connect_helper_closes(shop: Path) -> None:
    with connect(shop) as g:
        assert g.scalar("SELECT 1") == 1
    with pytest.raises(sqlite3.ProgrammingError):
        g.con.execute("SELECT 1")


def test_hot_rollback_journal_is_reported_and_left_alone(tmp_path: Path) -> None:
    """A crashed writer leaves a hot -journal. A read-only open cannot roll it back; we must say so, not touch it."""
    import shutil

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    c = sqlite3.connect(src / "h.db", isolation_level=None)
    c.execute("CREATE TABLE t (a TEXT)")
    c.execute("INSERT INTO t VALUES ('base')")
    c.execute("PRAGMA cache_size = 1")
    c.execute("PRAGMA cache_spill = 1")
    c.execute("BEGIN")
    c.executemany("INSERT INTO t VALUES (?)", [("x" * 500,) for _ in range(5000)])
    for f in src.iterdir():  # snapshot the files mid-transaction, as a crash would leave them
        shutil.copy(f, dst / f.name)
    c.execute("ROLLBACK")
    c.close()
    if not (dst / "h.db-journal").exists() or (dst / "h.db-journal").stat().st_size == 0:
        pytest.skip("this SQLite build did not spill the transaction to a journal")
    before = tree_state(dst)
    with pytest.raises(SqlScopeError, match="needed to write.*left untouched"):
        server.db_overview(str(dst / "h.db"))
    assert tree_state(dst) == before
    assert sorted(os.listdir(dst)) == ["h.db", "h.db-journal"]


@pytest.mark.skipif(os.name != "nt", reason="Windows device names")
def test_windows_device_names_are_not_databases() -> None:
    for name in ["NUL", "CON"]:
        with pytest.raises(SqlScopeError):
            server.db_overview(name)
