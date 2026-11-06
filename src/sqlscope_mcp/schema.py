"""Schema model built from sqlite_master and the introspection pragmas."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from .db import Guarded, SqlScopeError, qi

# Suffixes of the shadow tables SQLite's built-in virtual-table modules create.
_SHADOW_SUFFIXES = {
    "fts5": {"data", "idx", "content", "docsize", "config"},
    "fts4": {"content", "segments", "segdir", "docsize", "stat"},
    "fts3": {"content", "segments", "segdir"},
    "rtree": {"node", "rowid", "parent"},
    "rtree_i32": {"node", "rowid", "parent"},
    "geopoly": {"node", "rowid", "parent"},
}


@dataclass
class Column:
    cid: int
    name: str
    type: str
    notnull: bool
    default: str | None
    pk: int  # 0 = not in the primary key, else 1-based position
    hidden: int  # 0 normal, 1 hidden (virtual table), 2 virtual generated, 3 stored generated


@dataclass
class ForeignKey:
    id: int
    table: str
    from_cols: list[str]
    to_cols: list[str | None]
    on_update: str
    on_delete: str


@dataclass
class Index:
    name: str
    unique: bool
    origin: str  # c = CREATE INDEX, u = UNIQUE constraint, pk = PRIMARY KEY
    partial: bool
    columns: list[str | None]  # None for an expression column
    sql: str | None = None


@dataclass
class Table:
    name: str
    kind: str  # table | view
    sql: str
    module: str | None = None  # virtual table module
    shadow_of: str | None = None
    without_rowid: bool = False
    strict: bool = False
    columns: list[Column] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    triggers: list[tuple[str, str]] = field(default_factory=list)  # (name, sql)
    error: str | None = None

    @property
    def is_virtual(self) -> bool:
        return self.module is not None

    @property
    def pk_columns(self) -> list[str]:
        return [c.name for c in sorted((c for c in self.columns if c.pk), key=lambda c: c.pk)]

    @property
    def data_columns(self) -> list[Column]:
        """Columns that hold values (not the hidden helper columns of virtual tables)."""
        return [c for c in self.columns if c.hidden != 1]

    @property
    def has_rowid(self) -> bool:
        return self.kind == "table" and not self.without_rowid and not self.is_virtual and not any(
            c.name.lower() in ("rowid", "oid", "_rowid_") for c in self.columns
        )

    @property
    def is_rowid_alias_pk(self) -> bool:
        """True for ``INTEGER PRIMARY KEY`` on a rowid table (the PK is the rowid)."""
        pks = [c for c in self.columns if c.pk]
        return self.has_rowid and len(pks) == 1 and pks[0].type.upper() == "INTEGER"


_VIRTUAL = re.compile(r"CREATE\s+VIRTUAL\s+TABLE\s+.+?\s+USING\s+([A-Za-z0-9_]+)", re.I | re.S)


def _tail(sql: str) -> str:
    return sql[sql.rfind(")") + 1:] if ")" in sql else ""


def _load_table(g: Guarded, name: str, kind: str, sql: str) -> Table:
    t = Table(name=name, kind=kind, sql=sql or "")
    m = _VIRTUAL.match(t.sql)
    if m:
        t.module = m.group(1).lower()
    tail = _tail(t.sql)
    t.without_rowid = bool(re.search(r"\bWITHOUT\s+ROWID\b", tail, re.I))
    t.strict = bool(re.search(r"\bSTRICT\b", tail, re.I))
    try:
        rows = g.fetch("SELECT cid, name, type, \"notnull\", dflt_value, pk, hidden FROM pragma_table_xinfo(?)", (name,), max_rows=5000).rows
        t.columns = [Column(r[0], r[1], r[2] or "", bool(r[3]), r[4], r[5], r[6]) for r in rows]
        if kind == "table" and not t.is_virtual:
            fks: dict[int, ForeignKey] = {}
            for r in g.fetch('SELECT id, seq, "table", "from", "to", on_update, on_delete FROM pragma_foreign_key_list(?) ORDER BY id, seq', (name,), max_rows=5000).rows:
                fk = fks.setdefault(r[0], ForeignKey(r[0], r[2], [], [], r[5], r[6]))
                fk.from_cols.append(r[3])
                fk.to_cols.append(r[4])
            t.foreign_keys = list(fks.values())
            for r in g.fetch('SELECT name, "unique", origin, partial FROM pragma_index_list(?) ORDER BY seq', (name,), max_rows=5000).rows:
                cols = g.fetch("SELECT name FROM pragma_index_xinfo(?) WHERE key = 1 ORDER BY seqno", (r[0],), max_rows=500).rows
                isql = g.scalar("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (r[0],))
                t.indexes.append(Index(r[0], bool(r[1]), r[2], bool(r[3]), [c[0] for c in cols], isql))
    except SqlScopeError as e:
        # A virtual table whose module is missing (e.g. rtree not compiled in) cannot be introspected.
        t.error = str(e)
    return t


@dataclass
class Schema:
    tables: dict[str, Table]
    triggers: list[tuple[str, str, str]]  # (name, table, sql)
    index_count: int

    def find(self, name: str) -> Table:
        for key, t in self.tables.items():
            if key == name:
                return t
        lowered = [t for key, t in self.tables.items() if key.lower() == name.lower()]
        if len(lowered) == 1:
            return lowered[0]
        names = sorted(self.tables)
        close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        shown = ", ".join(names[:60]) + (f", ... ({len(names)} total)" if len(names) > 60 else "")
        raise SqlScopeError(f"No table or view named {name!r}.{hint} Available: {shown or '(none)'}.")

    def regular_tables(self) -> list[Table]:
        return [t for t in self.tables.values() if t.kind == "table" and not t.is_virtual and t.shadow_of is None]


def load_schema(g: Guarded) -> Schema:
    objs = g.fetch(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY type, name",
        max_rows=100_000,
    ).rows
    tables: dict[str, Table] = {}
    triggers: list[tuple[str, str, str]] = []
    index_count = 0
    for typ, name, tbl, sql in objs:
        if typ in ("table", "view"):
            tables[name] = _load_table(g, name, typ, sql or "")
        elif typ == "trigger":
            triggers.append((name, tbl, sql or ""))
        elif typ == "index" and sql is not None:
            index_count += 1
    # Explicit indexes are counted above; automatic ones come from the per-table lists.
    auto = sum(1 for t in tables.values() for i in t.indexes if i.origin != "c")
    index_count += auto
    modules = {t.name: t.module for t in tables.values() if t.module}
    for t in tables.values():
        if t.kind == "table" and not t.is_virtual:
            for vname, module in modules.items():
                if module and t.name.startswith(vname + "_") and t.name[len(vname) + 1:] in _SHADOW_SUFFIXES.get(module, ()):
                    t.shadow_of = vname
    for tname, ttable, tsql in triggers:
        if ttable in tables:
            tables[ttable].triggers.append((tname, tsql))
    return Schema(tables, triggers, index_count)


# ---------------------------------------------------------------- CHECK constraints


def check_constraints(sql: str) -> list[str]:
    """Extract the bodies of ``CHECK (...)`` constraints from a CREATE TABLE statement."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if c == "[":
            j = sql.find("]", i)
            i = n if j < 0 else j + 1
            continue
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        m = re.match(r"CHECK\s*\(", sql[i:i + 16], re.I) if c in "cC" and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")) else None
        if m:
            start = i + m.end()
            depth, j = 1, start
            while j < n and depth:
                ch = sql[j]
                if ch in "'\"`":
                    k = j + 1
                    while k < n and sql[k] != ch:
                        k += 1
                    j = k
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                j += 1
            out.append(" ".join(sql[start:j - 1].split()))
            i = j
            continue
        i += 1
    return out


def fk_index_covered(t: Table, cols: list[str]) -> bool:
    """Is there an index (or the PK) whose leading columns are exactly ``cols`` (any order)?"""
    want = {c.lower() for c in cols}
    pk = [c.lower() for c in t.pk_columns]
    if pk and set(pk[: len(want)]) == want and len(pk) >= len(want):
        return True
    for ix in t.indexes:
        lead = [(c or "").lower() for c in ix.columns[: len(want)]]
        if len(lead) == len(want) and set(lead) == want:
            return True
    return False


def parent_columns(schema: Schema, fk: ForeignKey) -> list[str] | None:
    """Resolve the parent columns of ``fk`` (an omitted list means the parent's PK)."""
    if all(c is not None for c in fk.to_cols):
        return [str(c) for c in fk.to_cols]
    parent = schema.tables.get(fk.table)
    if parent is None:
        for key, p in schema.tables.items():
            if key.lower() == fk.table.lower():
                parent = p
    if parent is None or not parent.pk_columns:
        return None
    pk = parent.pk_columns
    return pk if len(pk) == len(fk.from_cols) else None


def sample_source(g: Guarded, t: Table, sample: int) -> tuple[str, str]:
    """A FROM-clause fragment that yields at most about ``sample`` rows spread across the table.

    Returns (sql, description). Rowid tables are strided by rowid; others take the first rows.
    """
    q = qi(t.name)
    n = g.scalar(f"SELECT count(*) FROM (SELECT 1 FROM {q} LIMIT ?)", (sample + 1,), seconds=15)
    if n <= sample:
        return q, f"all {n} rows"
    if t.has_rowid:
        lo, hi = g.fetch(f"SELECT min(rowid), max(rowid) FROM {q}").rows[0]
        stride = max(2, (hi - lo + 1) // sample)
        return f"(SELECT * FROM {q} WHERE rowid % {int(stride)} = 0)", f"a sample of about {sample} rows (every {stride}th rowid)"
    return f"(SELECT * FROM {q} LIMIT {int(sample)})", f"the first {sample} rows"
