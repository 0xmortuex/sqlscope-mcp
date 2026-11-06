"""sqlscope-mcp: explore and understand an SQLite database file, read-only by construction."""

from __future__ import annotations

import argparse
import functools
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import analyze, explore
from .db import SqlScopeError, connect, ensure_select, resolve_path
from .render import cap_rows_by_bytes, clip, table

DEFAULT_MAX_ROWS = 200
HARD_MAX_ROWS = 5000
MAX_QUERY_BYTES = 200_000
MAX_QUERY_SECONDS = 60.0

mcp = MCPServer(
    "sqlscope",
    instructions=(
        "Explore and understand an SQLite database file. Everything is read-only by construction "
        "(the file is opened read-only and every statement passes an authorizer), so it is safe to "
        "point at any .db / .sqlite file. Every tool takes the file's path. Start with db_overview "
        "(size, tables, row counts), then describe_table for one table, schema_graph for how tables "
        "relate, and data_quality to find problems. Use query for SELECT statements (results are "
        "capped; aggregate instead of pulling rows), explain_query to see whether a query uses an "
        "index, find_in_db to locate a value anywhere in the database, and schema_docs to produce "
        "markdown documentation."
    ),
)

_root: Path | None = None
F = TypeVar("F", bound=Callable[..., Any])
_RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def configure(directory: str | None) -> Path | None:
    """Set the sandbox folder (None = any path). Called at startup and by tests."""
    global _root
    if directory is None:
        _root = None
        return None
    p = Path(directory).resolve()
    if not p.is_dir():
        raise SqlScopeError(f"--dir {directory!r} is not a folder.")
    _root = p
    return p


def tool(fn: F) -> F:
    # MCPServer only forwards a ToolError's message to the client, so the errors that carry
    # advice for the agent are re-raised as ToolError. Anything else is a genuine bug.
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except SqlScopeError as e:
            raise ToolError(str(e)) from e
        except ValueError as e:
            raise ToolError(str(e)) from e

    mcp.tool(annotations=_RO)(wrapper)
    return fn


def _clamp(value: int, low: int, high: int, name: str) -> int:
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high} (got {value}).")
    return value


@tool
def db_overview(path: str) -> str:
    """Start here. Summarise an SQLite database file: size, page size, text encoding, journal
    mode (WAL or not), user_version, counts of tables / views / indexes / triggers, and a row
    count for every table (from ANALYZE statistics when present, otherwise an exact count that
    stops at 100,000). Virtual tables (FTS5, rtree) are listed with their module and their
    internal shadow tables are folded away. Opening is read-only and never modifies the file or
    creates -wal / -shm files.

    path: the .db / .sqlite file (relative paths are resolved inside the --dir folder, if one was set).
    """
    with connect(resolve_path(path, _root)) as g:
        return clip(explore.db_overview(g))


@tool
def describe_table(path: str, table: str, sample_rows: int = 5, stats_sample: int = 10000) -> str:
    """Describe one table or view in depth: columns with declared type, NOT NULL, default, primary
    key and generated-column flags; per-column NULL percentage, distinct count, min, max and the
    storage types actually seen (computed over a sample of up to stats_sample rows); foreign keys
    in both directions; indexes; CHECK constraints; triggers; the CREATE statement; and sample
    rows. BLOBs are shown as size plus a hex prefix, long text is truncated with its length.

    table: table or view name (case-insensitive; a close-match hint is given if it does not exist).
    sample_rows: how many example rows to show (0-50).
    stats_sample: rows to sample for the statistics (100-200000).
    """
    _clamp(sample_rows, 0, 50, "sample_rows")
    _clamp(stats_sample, 100, 200_000, "stats_sample")
    with connect(resolve_path(path, _root)) as g:
        return clip(explore.describe_table(g, table, sample_rows, stats_sample))


@tool
def schema_graph(path: str, format: Literal["text", "mermaid"] = "text", orphan_sample: int = 1000) -> str:
    """Show how tables relate through foreign keys: every child -> parent edge, tables with no
    relationships, a parents-first load order and circular references. Also reports foreign-key
    columns that have no index (with a CREATE INDEX suggestion as text) and checks for orphan rows
    (child rows whose parent does not exist) over a sample, even when PRAGMA foreign_keys is off.

    format: "text" (default) or "mermaid" (an erDiagram you can paste into a renderer).
    orphan_sample: child rows to sample per foreign key (100-100000).
    """
    _clamp(orphan_sample, 100, 100_000, "orphan_sample")
    with connect(resolve_path(path, _root)) as g:
        return clip(explore.schema_graph(g, format, orphan_sample))


@tool
def query(
    path: str,
    sql: str,
    format: Literal["markdown", "csv", "json"] = "markdown",
    max_rows: int = DEFAULT_MAX_ROWS,
    timeout_seconds: float = 10.0,
) -> str:
    """Run one read-only SELECT / WITH / VALUES statement. Anything that would write (INSERT,
    UPDATE, DELETE, CREATE, DROP, ATTACH, VACUUM, PRAGMA assignments, load_extension) is
    refused. Results stop at max_rows rows and about 200 KB, and the query is interrupted after
    timeout_seconds. BLOB cells show size plus a hex prefix, and text longer than 200 characters
    is truncated with its length. Prefer aggregates (COUNT, GROUP BY) over pulling raw rows.

    format: markdown (default), csv or json.
    max_rows: 1-5000.
    timeout_seconds: 0.1-60.
    """
    _clamp(max_rows, 1, HARD_MAX_ROWS, "max_rows")
    if not 0.1 <= timeout_seconds <= MAX_QUERY_SECONDS:
        raise ValueError(f"timeout_seconds must be between 0.1 and {MAX_QUERY_SECONDS:g}.")
    ensure_select(sql)
    with connect(resolve_path(path, _root)) as g:
        r = g.fetch(sql, max_rows=max_rows, seconds=timeout_seconds)
    keep, byte_capped = cap_rows_by_bytes(r.rows, MAX_QUERY_BYTES, 200)
    rows = r.rows[:keep]
    if not r.columns:
        return "The statement returned no columns."
    body = table(format, r.columns, rows, 200)
    notes = [f"{len(rows)} row(s), {len(r.columns)} column(s), {r.elapsed * 1000:.0f} ms."]
    if r.truncated or byte_capped:
        why = f"the output reached the {MAX_QUERY_BYTES // 1000} KB cap" if byte_capped else f"max_rows={max_rows}"
        notes.append(f"TRUNCATED: more rows exist ({why}). Add LIMIT / WHERE / aggregation, or raise max_rows.")
    if format == "json":
        return clip(body + "\n" + " ".join(notes), MAX_QUERY_BYTES + 2000)
    return clip(body + "\n\n" + " ".join(notes), MAX_QUERY_BYTES + 2000)


@tool
def explain_query(path: str, sql: str) -> str:
    """Show the execution plan of a SELECT / WITH query without running it (EXPLAIN QUERY PLAN),
    as an indented tree, followed by plain-language findings: full table scans (with row counts),
    temporary B-tree sorts for ORDER BY / GROUP BY / DISTINCT, and automatic indexes SQLite has to
    build on every run. Missing-index suggestions are returned as CREATE INDEX text only; nothing
    is ever created.
    """
    with connect(resolve_path(path, _root)) as g:
        return clip(analyze.explain_query(g, sql))


@tool
def find_in_db(
    path: str,
    term: str,
    case_sensitive: bool = False,
    max_matches_per_table: int = 5,
    seconds_per_table: float = 3.0,
) -> str:
    """Find where a string or number appears anywhere in the database: a substring search over
    every non-BLOB column of every ordinary table (virtual and shadow tables are skipped).
    Returns, per table, the matching rows (rowid or primary key), the column and a snippet around
    the match. Each table has a time budget and a match cap, so a huge database cannot hang the
    call; tables that ran out of budget are listed.

    case_sensitive: default false (SQLite folds ASCII letters only, so Turkish or other non-ASCII
    letters match case-sensitively).
    max_matches_per_table: 1-50.
    seconds_per_table: 0.1-30.
    """
    _clamp(max_matches_per_table, 1, 50, "max_matches_per_table")
    if not 0.1 <= seconds_per_table <= 30:
        raise ValueError("seconds_per_table must be between 0.1 and 30.")
    with connect(resolve_path(path, _root)) as g:
        return clip(analyze.find_in_db(g, term, case_sensitive, max_matches_per_table, seconds_per_table))


@tool
def data_quality(path: str, table: str | None = None, sample: int = 20000) -> str:
    """Look for data problems, using SQL over a sample of each table: columns that are mostly or
    always NULL (and NULL versus empty-string mixes); columns holding mixed storage types (SQLite
    does not enforce column types; the typeof() distribution is reported, with examples);
    key-like columns (id, email, uuid, ...) that contain duplicates, case/space-only duplicates,
    and rows identical in every non-key column; dangling foreign keys even though SQLite does not
    enforce them by default; and date-looking text columns whose formats are inconsistent.

    table: check just this table (default: all ordinary tables).
    sample: rows to examine per table (100-200000).
    """
    _clamp(sample, 100, 200_000, "sample")
    with connect(resolve_path(path, _root)) as g:
        return clip(analyze.data_quality(g, table, sample))


@tool
def schema_docs(path: str) -> str:
    """Generate markdown documentation of the whole schema as text: an entity-relationship
    diagram (mermaid) when foreign keys exist, then every table with its columns, types, defaults,
    key and reference notes, indexes, CHECK constraints and triggers, followed by views and
    trigger definitions. Nothing is written to disk; paste the result where you want it.
    """
    with connect(resolve_path(path, _root)) as g:
        return clip(explore.schema_docs(g), 400_000)


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sqlscope-mcp", description="Read-only MCP server for exploring SQLite databases.")
    p.add_argument("--dir", help="only allow databases inside this folder (relative paths are resolved against it)")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse(argv)
    try:
        configure(args.dir)
    except SqlScopeError as e:
        sys.exit(f"sqlscope-mcp: {e}")
    mcp.run()


if __name__ == "__main__":
    main()
