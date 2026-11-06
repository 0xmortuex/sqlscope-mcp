"""Read-only access to an SQLite file.

Layers, outermost first:

1. File checks before SQLite sees the file (empty, not SQLite, probably encrypted).
2. The connection is opened with ``mode=ro`` (and ``immutable=1`` for a WAL-mode file
   that has no pending WAL, so opening it creates no ``-wal``/``-shm`` files).
3. ``PRAGMA query_only=ON`` and ``PRAGMA trusted_schema=OFF``.
4. An authorizer that allows only SELECT / READ / function calls / a short list of
   introspection pragmas. Everything else (ATTACH, writes, DDL, ``PRAGMA x=y``,
   ``load_extension``) is denied at prepare time.
5. A progress handler that enforces a wall-clock deadline, and a length limit.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HEADER = b"SQLite format 3\x00"
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3", ".sdb", ".s3db")
MAX_LENGTH = 64 * 1024 * 1024  # SQLITE_LIMIT_LENGTH: one value may not exceed 64 MiB
MAX_SQL_LENGTH = 1_000_000
PROGRESS_STEPS = 5000


class SqlScopeError(Exception):
    """A failure with a message written for the agent."""


# ---------------------------------------------------------------- identifiers


def qi(name: str) -> str:
    """Quote an identifier for SQL."""
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------- paths


def resolve_path(path: str, root: Path | None) -> Path:
    """Resolve ``path`` (relative paths are taken against the sandbox root, if any)."""
    if not path or not path.strip():
        raise SqlScopeError("path is empty. Pass the path of an SQLite database file.")
    if "\x00" in path:
        raise SqlScopeError("path contains a NUL character.")
    p = Path(path.strip())
    if root is not None:
        if not p.is_absolute():
            p = root / p
        resolved = p.resolve()
        if not resolved.is_relative_to(root):
            raise SqlScopeError(
                f"{path!r} is outside the allowed folder {root}. "
                "This server only opens databases inside the folder it was started with (--dir)."
            )
        return resolved
    return p.resolve()


def _candidates(folder: Path) -> list[str]:
    try:
        names = sorted(
            f.name for f in folder.iterdir() if f.is_file() and f.suffix.lower() in SQLITE_SUFFIXES
        )
    except OSError:
        return []
    return names[:20]


def check_file(path: Path) -> bytes:
    """Validate that ``path`` is a readable SQLite file; return its first 100 bytes."""
    if path.is_dir():
        found = _candidates(path)
        hint = f" SQLite-looking files in it: {', '.join(found)}." if found else ""
        raise SqlScopeError(f"{path} is a folder, not a database file.{hint}")
    if not path.exists():
        hint = ""
        if path.parent.is_dir():
            found = _candidates(path.parent)
            if found:
                hint = f" SQLite-looking files in {path.parent}: {', '.join(found)}."
        raise SqlScopeError(f"No such file: {path}.{hint}")
    if not path.is_file():
        raise SqlScopeError(f"{path} is not a regular file (device, pipe or socket), so it cannot be an SQLite database.")
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(100)
    except OSError as e:
        raise SqlScopeError(f"Cannot read {path}: {e.strerror or e}") from e
    if size == 0:
        raise SqlScopeError(
            f"{path} is empty (0 bytes). An SQLite database always has at least one page; "
            "this is a placeholder file that was never written to."
        )
    if not head.startswith(HEADER):
        distinct = len(set(head[:16]))
        printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in head[:16])
        if len(head) >= 16 and distinct >= 12 and printable < 12:
            raise SqlScopeError(
                f"{path} is not a readable SQLite database: its first 16 bytes look random, "
                "which is what an encrypted database (SQLCipher / SEE) looks like. "
                "sqlscope-mcp cannot open encrypted databases; decrypt a copy first."
            )
        raise SqlScopeError(
            f"{path} is not an SQLite database: it does not start with the 'SQLite format 3' "
            "header. It may be another format (DuckDB, CSV, ...) or a corrupt file."
        )
    if len(head) < 100:
        raise SqlScopeError(f"{path} is truncated: the SQLite header is incomplete ({len(head)} bytes).")
    page_size = int.from_bytes(head[16:18], "big")
    if page_size == 1:
        page_size = 65536
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise SqlScopeError(f"{path} has a damaged SQLite header (invalid page size {page_size}).")
    return head


def _uri(path: Path, immutable: bool) -> str:
    return path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")


# ---------------------------------------------------------------- authorizer


def _const(name: str) -> int:
    value = getattr(sqlite3, name)
    assert isinstance(value, int)
    return value


_OK = _const("SQLITE_OK")
_DENY = _const("SQLITE_DENY")
_ALLOWED_ACTIONS = frozenset(
    _const(n) for n in ("SQLITE_SELECT", "SQLITE_READ", "SQLITE_FUNCTION", "SQLITE_RECURSIVE")
)
_PRAGMA = _const("SQLITE_PRAGMA")
_ACTION_NAMES = {
    _const(f"SQLITE_{n}"): n
    for n in (
        "CREATE_INDEX CREATE_TABLE CREATE_TEMP_INDEX CREATE_TEMP_TABLE CREATE_TEMP_TRIGGER "
        "CREATE_TEMP_VIEW CREATE_TRIGGER CREATE_VIEW DELETE DROP_INDEX DROP_TABLE DROP_TEMP_INDEX "
        "DROP_TEMP_TABLE DROP_TEMP_TRIGGER DROP_TEMP_VIEW DROP_TRIGGER DROP_VIEW INSERT PRAGMA "
        "UPDATE ATTACH DETACH ALTER_TABLE REINDEX ANALYZE CREATE_VTABLE DROP_VTABLE SAVEPOINT "
        "TRANSACTION"
    ).split()
}
_DENIED_FUNCTIONS = frozenset({"load_extension", "fts3_tokenizer"})
# Pragmas that only read. Allowed when called without an argument.
_PRAGMAS_NOARG = frozenset(
    """page_size page_count freelist_count encoding journal_mode user_version application_id
    schema_version data_version max_page_count foreign_keys auto_vacuum database_list
    collation_list compile_options function_list module_list pragma_list table_list""".split()
)
# Pragmas that take a table / index name and return information about it.
_PRAGMAS_WITH_ARG = frozenset(
    "table_info table_xinfo table_list foreign_key_list index_list index_info index_xinfo".split()
)


def _auth_decision(action: int, arg1: str | None, arg2: str | None) -> tuple[int, str]:
    if action in _ALLOWED_ACTIONS:
        if action == _const("SQLITE_FUNCTION") and (arg2 or "").lower() in _DENIED_FUNCTIONS:
            return _DENY, f"function {arg2}()"
        return _OK, ""
    if action == _PRAGMA:
        name = (arg1 or "").lower()
        if (name in _PRAGMAS_NOARG and arg2 is None) or name in _PRAGMAS_WITH_ARG:
            return _OK, ""
        setting = f"PRAGMA {arg1}" + (f" = {arg2}" if arg2 is not None else "")
        return _DENY, setting
    return _DENY, _ACTION_NAMES.get(action, f"action {action}")


# ---------------------------------------------------------------- results


@dataclass
class Rows:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool = False  # more rows existed than max_rows
    timed_out: bool = False  # the deadline fired while reading (partial result)
    elapsed: float = 0.0
    notes: list[str] = field(default_factory=list)


_FRIENDLY = (
    ("file is not a database", "The file is not a readable SQLite database (it may be encrypted, e.g. SQLCipher, or corrupt)."),
    ("database disk image is malformed", "The database file is corrupt ('database disk image is malformed')."),
    ("attempt to write a readonly database", "SQLite needed to write to this database (a hot rollback journal or an unrecoverable WAL?). It was left untouched; open it once with a normal SQLite client to let it recover, then retry."),
    ("unable to open database file", "SQLite could not open the file (permissions, a locked file or a vanished path)."),
    ("database is locked", "The database is locked by another process; try again shortly."),
)


class Guarded:
    """One read-only connection with limits. Create it with :func:`open_db`."""

    def __init__(self, con: sqlite3.Connection, path: Path, immutable: bool) -> None:
        self.con = con
        self.path = path
        self.immutable = immutable
        self.denied: str | None = None
        self._deadline: float | None = None
        con.set_authorizer(self._authorize)
        con.set_progress_handler(self._progress, PROGRESS_STEPS)

    def _authorize(self, action: int, arg1: str | None, arg2: str | None, db: str | None, trigger: str | None) -> int:
        decision, what = _auth_decision(action, arg1, arg2)
        if decision == _DENY:
            self.denied = what
        return decision

    def _progress(self) -> int:
        if self._deadline is not None and time.monotonic() > self._deadline:
            return 1
        return 0

    def translate(self, e: sqlite3.Error, seconds: float | None = None) -> SqlScopeError:
        text = str(e)
        low = text.lower()
        if "not authorized" in low or "authorization denied" in low:
            what = self.denied or "that operation"
            return SqlScopeError(
                f"Denied: {what}. This server is read-only by construction: only SELECT queries "
                "and read-only introspection are allowed."
            )
        if "interrupted" in low:
            limit = f"{seconds:g} s" if seconds is not None else "the time limit"
            return SqlScopeError(
                f"The query ran longer than {limit} and was interrupted. Add a WHERE clause or "
                "LIMIT, or query an indexed column (explain_query shows the plan)."
            )
        for needle, message in _FRIENDLY:
            if needle in low:
                return SqlScopeError(message)
        if "string or blob too big" in low:
            return SqlScopeError("A value in the result is larger than 64 MiB, which this server refuses to load.")
        return SqlScopeError(f"SQLite error: {text}")

    @contextmanager
    def budget(self, seconds: float | None) -> Iterator[None]:
        previous = self._deadline
        self._deadline = None if seconds is None else time.monotonic() + seconds
        try:
            yield
        finally:
            self._deadline = previous

    def fetch(
        self,
        sql: str,
        params: Sequence[Any] = (),
        max_rows: int = 1000,
        seconds: float | None = 10.0,
        partial_ok: bool = False,
    ) -> Rows:
        """Run one statement; read at most ``max_rows`` rows.

        With ``partial_ok`` a timeout returns the rows read so far (``timed_out=True``)
        instead of raising.
        """
        started = time.monotonic()
        self.denied = None
        rows: list[tuple[Any, ...]] = []
        columns: list[str] = []
        truncated = False
        timed_out = False
        try:
            with self.budget(seconds):
                try:
                    cur = self.con.execute(sql, tuple(params))
                    columns = [d[0] for d in (cur.description or [])]
                    while len(rows) <= max_rows:
                        chunk = cur.fetchmany(min(500, max_rows + 1 - len(rows)))
                        if not chunk:
                            break
                        rows.extend(chunk)
                    cur.close()
                except sqlite3.OperationalError as e:
                    if partial_ok and "interrupted" in str(e).lower():
                        timed_out = True
                    else:
                        raise
                if len(rows) > max_rows:
                    truncated = True
                    del rows[max_rows:]
        except sqlite3.Error as e:
            raise self.translate(e, seconds) from e
        except sqlite3.Warning as e:  # "You can only execute one statement at a time"
            raise SqlScopeError(f"SQLite rejected the SQL: {e}") from e
        return Rows(columns, rows, truncated, timed_out, time.monotonic() - started)

    def scalar(self, sql: str, params: Sequence[Any] = (), seconds: float | None = 10.0) -> Any:
        r = self.fetch(sql, params, max_rows=1, seconds=seconds)
        return r.rows[0][0] if r.rows else None

    def close(self) -> None:
        self.con.close()


def open_db(path: Path) -> Guarded:
    """Open ``path`` read-only. Raises :class:`SqlScopeError` with a clear message."""
    head = check_file(path)
    wal = head[18] == 2 or head[19] == 2
    immutable = False
    if wal:
        wal_file = Path(str(path) + "-wal")
        try:
            wal_size = wal_file.stat().st_size
        except FileNotFoundError:
            wal_size = 0
        # With no pending WAL there is nothing to merge in, and a plain read-only open of a
        # WAL file would create -wal and -shm next to it. immutable=1 avoids both.
        immutable = wal_size == 0
    try:
        con = sqlite3.connect(
            _uri(path, immutable), uri=True, isolation_level=None, check_same_thread=False
        )
    except sqlite3.Error as e:
        raise SqlScopeError(f"Cannot open {path}: {e}") from e
    try:
        con.text_factory = lambda b: b.decode("utf-8", errors="replace")
        con.execute("PRAGMA query_only = ON")
        con.execute("PRAGMA trusted_schema = OFF")
        con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_LENGTH)
        con.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_LENGTH)
        con.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        g = Guarded(con, path, immutable)
        g.fetch("SELECT count(*) FROM sqlite_master", seconds=10)  # fails early on bad files
    except SqlScopeError:
        con.close()
        raise
    except sqlite3.Error as e:
        con.close()
        raise SqlScopeError(f"Cannot read {path}: {e}") from e
    return g


@contextmanager
def connect(path: Path) -> Iterator[Guarded]:
    g = open_db(path)
    try:
        yield g
    finally:
        g.close()


# ---------------------------------------------------------------- SQL gate


_LEADING = re.compile(r"\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/)\s*", re.S)


def ensure_select(sql: str) -> str:
    """Return ``sql`` if it is one SELECT / WITH / VALUES statement, else raise.

    This is a friendly first gate; the authorizer is what actually enforces read-only.
    """
    if not sql or not sql.strip():
        raise SqlScopeError("sql is empty.")
    s = sql
    while True:
        m = _LEADING.match(s)
        if not m or m.end() == 0:
            break
        s = s[m.end():]
    first = re.match(r"[A-Za-z]+", s.lstrip("( \t\r\n"))
    word = first.group(0).upper() if first else ""
    if word not in ("SELECT", "WITH", "VALUES"):
        hint = ""
        if word == "EXPLAIN":
            hint = " Use the explain_query tool for query plans."
        elif word == "PRAGMA":
            hint = " Use db_overview / describe_table for pragma-level information."
        raise SqlScopeError(
            f"Only SELECT / WITH / VALUES queries are allowed (got {word or 'something else'}). "
            "This server is read-only by construction." + hint
        )
    return sql


# ---------------------------------------------------------------- cell shaping


def fmt_cell(value: Any, max_chars: int = 200) -> str:
    """Render one SQLite value for a human or model: blobs as size + hex prefix, long text cut."""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        prefix = value[:16].hex()
        more = "..." if len(value) > 16 else ""
        return f"<BLOB {len(value)} bytes 0x{prefix}{more}>"
    if isinstance(value, float):
        if math.isinf(value) or math.isnan(value):
            return str(value)
        return repr(value)
    text = str(value)
    if len(text) > max_chars:
        return f"{text[:max_chars]}... [{len(text)} chars total]"
    return text


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def file_size(path: Path) -> int:
    return os.stat(path).st_size
