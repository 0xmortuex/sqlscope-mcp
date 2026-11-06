# sqlscope-mcp

**Let your AI agent explore and understand an SQLite database file, without any way to change it.**

<!-- mcp-name: io.github.0xmortuex/sqlscope-mcp -->

sqlscope-mcp is an [MCP](https://modelcontextprotocol.io) server for SQLite files. Point it at
an app's `.db`, a browser profile, a game save or a data export, and the agent can see what is
in there, how the tables relate, whether the data is healthy and why a query is slow.

**How it differs from other SQLite MCP servers.** Several good read-only SQLite servers exist
([mcp-sqlite-insights](https://github.com/RayaanKaz/mcp-sqlite-insights),
[sqlite-explorer-fastmcp](https://github.com/hannesrudolph/sqlite-explorer-fastmcp-mcp-server),
[mcp-sqlite-explorer](https://github.com/Sarmkadan/mcp-sqlite-explorer),
[mcp-sql-explorer](https://glama.ai/mcp/servers/rahulkumar96t/mcp-sql-explorer)); going by their
READMEs they cover schema listing, sampling, a read-only query and some of them plan output. This one
is built around two things they do not advertise:

1. **Read-only is a tested property, not a promise.** Five layers (read-only URI, `query_only`,
   an authorizer allow-list, a deadline, a value-size limit) and an attack suite that fires
   53 hostile statements and checks the file hash and the folder listing are unchanged
   afterwards. That includes WAL-mode databases, where a naive read-only open silently creates
   `-wal` and `-shm` files next to your database.
2. **Insight tools, not just a query box.** A foreign-key graph that finds unindexed FK columns and
   orphan rows, `typeof()` distributions that expose SQLite's dynamic typing, dangling references
   with `foreign_keys` off, inconsistent date formats, whole-database search, plan findings with
   index suggestions, and generated markdown docs.

Only the Python standard library's `sqlite3` and the `mcp` SDK are used. No native dependencies.

## What it looks like

Real output from a stdio session against a small shop database:

```
> schema_graph
Foreign keys (3), child -> parent:
  order_items (sku) -> products(sku)
  order_items (order_id) -> orders(id)
  orders (customer_id) -> customers(id)  [ON DELETE CASCADE]

Missing indexes on foreign-key columns:
  orders(customer_id) has no index. Suggestion: CREATE INDEX "idx_orders_customer_id" ON "orders" ("customer_id");

Orphan check (child rows whose parent is missing; independent of PRAGMA foreign_keys):
  orders(customer_id) -> customers: 2 of 302 non-null values have no parent (0.7%); e.g. 99999; 88888
  order_items(sku) -> products: 1 of 4 non-null values have no parent (25.0%); e.g. ZZZ

> data_quality table=customers
  [WARN] notes: 99% NULL (198 of 200)
  [WARN] email looks like a key but is not unique: 1 value(s) repeat (1 extra rows), e.g. 'user5@example.com' x2
  [WARN] email: 2 value(s) collide after lower() and trim() (2 extra rows; 1 are exact duplicates)
  [WARN] created: date-looking text is inconsistent: 4 different formats: '9999-99-99' x197, '99.99.9999' x1, '99/99/9999' x1, ...

> explain_query "SELECT * FROM orders WHERE customer_id = 5"
Query plan:
  - SCAN orders
Findings:
  * FULL TABLE SCAN of orders (302 rows): every row is read.
Index suggestions (text only, nothing was created):
  * orders is scanned and the query filters on customer_id: CREATE INDEX "idx_orders_customer_id" ON "orders" ("customer_id");

> query "DROP TABLE orders"
Error: Only SELECT / WITH / VALUES queries are allowed (got DROP). This server is read-only by construction.
```

## Install

You need [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer.

**Claude Code**

```bash
claude mcp add sqlscope -- uvx sqlscope-mcp --dir "C:\Users\me\databases"
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "sqlscope": {
      "command": "uvx",
      "args": ["sqlscope-mcp", "--dir", "C:\\Users\\me\\databases"]
    }
  }
}
```

Or with pip: `pip install sqlscope-mcp`, then run `sqlscope-mcp --dir PATH`.

`--dir` is optional but recommended: with it, every tool refuses paths outside that folder
(`..`, absolute paths and symlinks are resolved first) and relative paths are taken inside it.
Without it, any file the server process can read may be opened.

## Tools

Every tool takes `path`, the database file.

| Tool | What it does |
|------|--------------|
| `db_overview` | File size, page size, encoding, journal mode, `user_version`, counts of tables / views / indexes / triggers, and a row count for every table (from `ANALYZE` statistics if present, else an exact count that stops at 100,000). Virtual tables listed, their shadow tables folded away. |
| `describe_table` | Columns with type, NOT NULL, default, PK and generated flags; NULL %, distinct, min, max and `typeof()` types per column over a sample; foreign keys in both directions; indexes; CHECK constraints; triggers; the CREATE statement; sample rows. |
| `schema_graph` | Foreign-key graph as text or mermaid, load order, cycles, FK columns with no index (with `CREATE INDEX` text), and orphan rows over a sample. |
| `query` | One `SELECT` / `WITH` / `VALUES` statement. Markdown (default), CSV or JSON. Row, byte and time caps with a TRUNCATED notice. |
| `explain_query` | `EXPLAIN QUERY PLAN` as a tree plus plain findings: full scans (with row counts), temp B-tree sorts, automatic indexes, and index suggestions as text. The query is not run. |
| `find_in_db` | Find a string or number in any non-BLOB column of any table, with a match cap and a time budget per table. |
| `data_quality` | NULL-heavy and always-NULL columns, NULL vs `''` mixes, mixed storage types per column, key-like columns with duplicates (also after `lower()`/`trim()`), rows identical apart from a surrogate key, dangling foreign keys, inconsistent date text formats. |
| `schema_docs` | Markdown documentation of the whole schema as text, with a mermaid ER diagram. Nothing is written to disk. |

Handled on purpose: BLOBs are shown as `<BLOB n bytes 0x...>` and never dumped; very long text is
cut with its length; FTS5 and other virtual tables are listed and queryable but not scanned;
WITHOUT ROWID, STRICT and generated columns; Turkish, quoted and otherwise odd identifiers;
UTF-16 databases; invalid UTF-8 in text; and WAL databases (a pending `-wal` is read, nothing is
written or checkpointed).

Files that are not usable produce a specific message instead of a stack trace: zero bytes,
not SQLite, probably encrypted (SQLCipher), truncated header, corrupt pages, and a hot rollback
journal left by a crashed writer.

## Try asking

- "Give me an overview of `app.db`. What are the biggest tables?"
- "Describe the `orders` table and show me a few rows."
- "How do the tables in this database relate? Draw it as a mermaid diagram."
- "Which foreign keys have no index? Are there any orphan rows?"
- "Is there anything wrong with the data in this database?"
- "Where does the email `ada@example.com` appear in this database?"
- "Why is this query slow?" (it will use `explain_query`)
- "Write schema documentation for this database that I can paste into a README."

## Security

What is enforced, and by what:

| Threat | Defence |
|--------|---------|
| Any write: `INSERT`, `UPDATE`, `DELETE`, DDL, `REINDEX`, `ANALYZE` | Connection opened with `mode=ro`, `PRAGMA query_only=ON`, and an authorizer that denies every action except SELECT / READ / function calls / recursion. |
| `ATTACH` (a second file, or `:memory:`), `DETACH` | Denied by the authorizer; the attach limit is also set to 0. |
| `VACUUM INTO 'file'` (the one statement a read-only connection can still use to write a new file) | Denied by the authorizer. The statement gate also rejects it. |
| `PRAGMA writable_schema=ON`, `journal_mode=`, `user_version=`, `query_only=OFF` | Denied: only a short list of read-only pragmas is allowed, and only in read form. |
| Temp tables, views, triggers | `CREATE TEMP ...` is a denied action. |
| `load_extension()`, `fts3_tokenizer()` | Denied by function name; extension loading is also never enabled. |
| Stacked statements (`SELECT 1; DROP TABLE t`) | One statement per call; the `query` and `explain_query` tools also accept only `SELECT` / `WITH` / `VALUES` as the first word. |
| Creating `-wal` / `-shm` / `-journal` files next to your database | A WAL file with no pending WAL is opened `immutable=1`, a rollback-journal file needs no side files, and a pending WAL is read without being checkpointed. A hot rollback journal is reported, never rolled back. |
| Runaway queries (`WITH RECURSIVE ... ` with no end, cross joins) | A progress handler interrupts after `timeout_seconds` (default 10, max 60). |
| Memory bombs (`zeroblob(2e9)`, `hex(randomblob(1e8))`) | `SQLITE_LIMIT_LENGTH` is 64 MiB, so such values fail instead of allocating. |
| Flooding the model's context | Row cap (default 200, max 5000), about 200 KB output cap, 200-character cell cap, BLOBs never dumped. |
| Opening files outside a folder | `--dir`: paths are resolved (`..`, symlinks, other drives) and must stay inside. |

The attack suite in `tests/test_readonly.py` runs these against real databases and compares a
SHA-256 of every file in the folder before and after.

What it does **not** do:

- **Read-only does not mean private.** A database can contain secrets, and every row a tool returns
  becomes part of your conversation with the model. Do not point it at a file you would not paste.
- `-shm` (the WAL index) can be touched by SQLite itself when a database has a pending WAL that
  another process is using; the database file and the `-wal` are never modified. The tests compare
  those two, not `-shm`.
- It uses the SQLite library bundled with your Python, so features newer than that library are
  unavailable.
- Encrypted databases are not supported; they are detected and reported.
- A single value larger than 64 MiB cannot be read.
- Without `--dir` the server can open any file the process can read (it still only reads).

## Development

```bash
uv sync --extra test
uv run ruff check src tests && uv run mypy --strict src
uv run pytest tests
```

Tests build real databases with `sqlite3`: a shop schema with seeded problems, WAL databases (with
and without a live writer), FTS5 (skipped if the build lacks it), a database with a hot journal,
broken and encrypted-looking files, a 1,000,000-row table for the cap and time checks, the attack
suite, and a stdio test that launches the installed server and talks MCP to it.

Want to know how it works? [sqlscope-mcp-the-hard-way](https://github.com/0xmortuex/sqlscope-mcp-the-hard-way)
builds a minimal version step by step.

## License

MIT
