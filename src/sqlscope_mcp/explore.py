"""db_overview, describe_table, schema_graph and schema_docs."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .db import Guarded, SqlScopeError, file_size, fmt_cell, human_size, qi
from .render import markdown_table
from .schema import (
    ForeignKey,
    Schema,
    Table,
    check_constraints,
    fk_index_covered,
    load_schema,
    parent_columns,
    sample_source,
)

COUNT_CAP = 100_000
COUNT_SECONDS = 2.0
ENCODINGS = {"UTF-8": "UTF-8", "UTF-16le": "UTF-16 little-endian", "UTF-16be": "UTF-16 big-endian"}


def _n(n: int) -> str:
    return f"{n:,}"


# ---------------------------------------------------------------- row counts


def stat1_counts(g: Guarded) -> dict[str, int]:
    """Row counts recorded by ANALYZE (sqlite_stat1), if the database has them."""
    if not g.scalar("SELECT count(*) FROM sqlite_master WHERE name = 'sqlite_stat1'"):
        return {}
    out: dict[str, int] = {}
    for tbl, stat in g.fetch("SELECT tbl, stat FROM sqlite_stat1", max_rows=100_000).rows:
        first = str(stat).split()[0] if stat else ""
        if first.isdigit():
            out.setdefault(tbl, int(first))
    return out


def count_label(g: Guarded, t: Table, stats: dict[str, int], cap: int = COUNT_CAP) -> str:
    """Row count: ANALYZE statistics if present, else an exact count that stops at ``cap``."""
    if t.kind == "view":
        return "(view)"
    if t.error:
        return "(unavailable)"
    if t.name in stats:
        return f"~{_n(stats[t.name])} (stat1)"
    try:
        n = g.scalar(f"SELECT count(*) FROM (SELECT 1 FROM {qi(t.name)} LIMIT ?)", (cap + 1,), seconds=COUNT_SECONDS)
    except SqlScopeError as e:
        return f"(count failed: {str(e)[:60]})"
    return f">={_n(cap)}" if n > cap else _n(n)


# ---------------------------------------------------------------- db_overview


def db_overview(g: Guarded) -> str:
    def pv(name: str) -> Any:
        return g.scalar(f"SELECT * FROM pragma_{name}")

    page_size, page_count, free = pv("page_size"), pv("page_count"), pv("freelist_count")
    encoding, journal = pv("encoding"), pv("journal_mode")
    if g.immutable:
        journal = "wal"  # the immutable open ignores the WAL, so the pragma would say 'delete'
    uv, app, auto = pv("user_version"), pv("application_id"), pv("auto_vacuum")
    size = file_size(g.path)
    schema = load_schema(g)
    stats = stat1_counts(g)

    tables = [t for t in schema.tables.values() if t.kind == "table"]
    views = [t for t in schema.tables.values() if t.kind == "view"]
    virtual = [t for t in tables if t.is_virtual]
    shadow = [t for t in tables if t.shadow_of]

    lines = [f"Database: {g.path}"]
    lines.append(f"File size: {human_size(size)} ({_n(size)} bytes), {_n(page_count)} pages x {page_size} bytes")
    free_note = f", {_n(free)} free pages ({free * page_size / size:.1%} reclaimable by VACUUM)" if free else ""
    lines.append(f"Encoding: {ENCODINGS.get(encoding, encoding)}, journal mode: {journal}{free_note}")
    lines.append(
        f"user_version: {uv}, application_id: {app} ({app & 0xFFFFFFFF:#010x}), "
        f"auto_vacuum: {('none', 'full', 'incremental')[auto] if 0 <= auto <= 2 else auto}"
    )
    if str(journal).lower() == "wal":
        wal = g.path.with_name(g.path.name + "-wal")
        if wal.exists() and wal.stat().st_size:
            lines.append(f"WAL: {human_size(wal.stat().st_size)} of pending changes are included in what you see (opened read-only, file untouched).")
        else:
            lines.append("WAL: no pending -wal file; opened as an immutable snapshot so no -wal/-shm files are created.")
    lines.append(
        f"Objects: {len(tables) - len(shadow)} tables ({len(virtual)} virtual"
        + (f", plus {len(shadow)} shadow tables of virtual tables" if shadow else "")
        + f"), {len(views)} views, {schema.index_count} indexes, {len(schema.triggers)} triggers"
    )
    lines.append("")

    rows = []
    for t in schema.tables.values():
        if t.shadow_of:
            continue
        kind = t.kind
        if t.module:
            kind = f"virtual ({t.module})"
        else:
            extras = [x for x, on in (("WITHOUT ROWID", t.without_rowid), ("STRICT", t.strict)) if on]
            if extras:
                kind += " " + " ".join(extras)
        notes = []
        if t.error:
            notes.append(t.error[:80])
        if t.foreign_keys:
            notes.append(f"{len(t.foreign_keys)} FK")
        if t.triggers:
            notes.append(f"{len(t.triggers)} trigger(s)")
        rows.append((t.name, kind, count_label(g, t, stats), len(t.data_columns), "; ".join(notes)))
    if rows:
        lines.append(markdown_table(["name", "kind", "rows", "columns", "notes"], rows, 80))
    else:
        lines.append("The database has no tables or views.")
    if shadow:
        by: dict[str, list[str]] = defaultdict(list)
        for t in shadow:
            by[t.shadow_of or ""].append(t.name)
        lines.append("")
        lines.append("Shadow tables (internal storage of virtual tables, ignore them): " + "; ".join(f"{k}: {', '.join(v)}" for k, v in by.items()))
    if stats:
        lines.append("")
        lines.append("Row counts marked (stat1) are estimates from the last ANALYZE; the others are exact counts (stopping at 100,000).")
    return "\n".join(lines)


# ---------------------------------------------------------------- describe_table


def _fk_text(fk: ForeignKey) -> str:
    to = ", ".join(c if c else "(primary key)" for c in fk.to_cols)
    acts = []
    if fk.on_delete not in ("NO ACTION", ""):
        acts.append(f"ON DELETE {fk.on_delete}")
    if fk.on_update not in ("NO ACTION", ""):
        acts.append(f"ON UPDATE {fk.on_update}")
    return f"({', '.join(fk.from_cols)}) -> {fk.table}({to})" + (f"  [{', '.join(acts)}]" if acts else "")


def describe_table(g: Guarded, table: str, sample_rows: int, stats_sample: int) -> str:
    schema = load_schema(g)
    t = schema.find(table)
    stats = stat1_counts(g)
    what = t.kind
    if t.module:
        what = f"virtual table using {t.module}"
    else:
        what += "".join(f", {x}" for x, on in (("WITHOUT ROWID", t.without_rowid), ("STRICT", t.strict)) if on)
    lines = [f"{t.kind.capitalize()} {t.name} ({what})", f"Rows: {count_label(g, t, stats)}"]
    if t.shadow_of:
        lines.append(f"This is an internal shadow table of the virtual table {t.shadow_of}.")
    if t.error:
        lines.append(f"Cannot introspect this object: {t.error}")
        lines.append(f"Definition: {t.sql}")
        return "\n".join(lines)

    # per-column statistics over a sample
    cols = t.data_columns
    stat_rows: dict[str, tuple[str, str, str, str, str]] = {}
    sample_note = ""
    if cols and not t.module and len(cols) <= 150:
        src, sample_note = sample_source(g, t, stats_sample)
        parts = []
        for c in cols:
            q = qi(c.name)
            parts.append(f"sum({q} IS NULL), count(DISTINCT {q}), min({q}), max({q}), group_concat(DISTINCT typeof({q}))")
        r = g.fetch(f"SELECT count(*), {', '.join(parts)} FROM {src}", seconds=15).rows[0]
        total = r[0]
        for i, c in enumerate(cols):
            nulls, distinct, mn, mx, types = r[1 + 5 * i: 6 + 5 * i]
            nulls = nulls or 0
            pct = f"{nulls / total:.0%} ({nulls})" if total else "-"
            stat_rows[c.name] = (pct, str(distinct), fmt_cell(mn, 40), fmt_cell(mx, 40), types or "-")
    elif len(cols) > 150:
        sample_note = "statistics skipped: more than 150 columns"

    rows = []
    for c in cols:
        flags = []
        if c.pk:
            flags.append(f"PK{c.pk if len(t.pk_columns) > 1 else ''}")
        if c.notnull:
            flags.append("NOT NULL")
        if c.hidden in (2, 3):
            flags.append("generated " + ("virtual" if c.hidden == 2 else "stored"))
        s = stat_rows.get(c.name, ("", "", "", "", ""))
        rows.append((c.name, c.type or "(none)", " ".join(flags), "" if c.default is None else c.default, *s))
    lines.append("")
    lines.append(markdown_table(["column", "type", "constraints", "default", "null", "distinct", "min", "max", "types seen"], rows, 120))
    if sample_note:
        lines.append(f"Statistics are computed over {sample_note}; distinct counts are exact within that sample.")
    for c in cols:
        if c.hidden in (2, 3):
            m = re.search(re.escape(c.name) + r"[^,]*?\bAS\s*\((.*?)\)\s*(?:VIRTUAL|STORED|,|\))", t.sql, re.I | re.S)
            if m:
                lines.append(f"Generated column {c.name}: AS ({' '.join(m.group(1).split())})")

    if t.pk_columns:
        lines.append(f"\nPrimary key: ({', '.join(t.pk_columns)})" + (" - alias of the rowid" if t.is_rowid_alias_pk else ""))
    elif t.kind == "table" and not t.module:
        lines.append("\nPrimary key: none")
    if t.foreign_keys:
        lines.append("\nForeign keys:")
        lines += [f"  {_fk_text(fk)}" for fk in t.foreign_keys]
    inbound = [(o.name, fk) for o in schema.tables.values() for fk in o.foreign_keys if fk.table.lower() == t.name.lower()]
    if inbound:
        lines.append("\nReferenced by:")
        lines += [f"  {n}({', '.join(fk.from_cols)})" for n, fk in inbound]
    if t.indexes:
        lines.append("\nIndexes:")
        for ix in t.indexes:
            cols_txt = ", ".join(c if c else "<expression>" for c in ix.columns)
            kind = {"c": "index", "u": "UNIQUE constraint", "pk": "PRIMARY KEY"}.get(ix.origin, ix.origin)
            lines.append(f"  {ix.name}: ({cols_txt}){' UNIQUE' if ix.unique and ix.origin == 'c' else ''}{' PARTIAL' if ix.partial else ''} [{kind}]")
    checks = check_constraints(t.sql) if t.kind == "table" and not t.module else []
    if checks:
        lines.append("\nCHECK constraints:")
        lines += [f"  CHECK ({c})" for c in checks]
    if t.triggers:
        lines.append("\nTriggers:")
        lines += [f"  {n}" for n, _ in t.triggers]
    lines.append("\nDefinition:")
    lines.append(t.sql if len(t.sql) <= 3000 else t.sql[:3000] + f"... [{len(t.sql)} chars total]")

    if sample_rows > 0 and cols:
        sel = ", ".join(qi(c.name) for c in cols)
        smp = g.fetch(f"SELECT {sel} FROM {qi(t.name)} LIMIT ?", (sample_rows,), max_rows=sample_rows, seconds=10)
        lines.append(f"\nSample rows (first {len(smp.rows)}):")
        lines.append(markdown_table([c.name for c in cols], smp.rows, 80) if smp.rows else "(the table is empty)")
    return "\n".join(lines)


# ---------------------------------------------------------------- schema_graph


def fk_edges(schema: Schema) -> list[tuple[Table, ForeignKey]]:
    return [(t, fk) for t in schema.tables.values() if t.kind == "table" for fk in t.foreign_keys]


def orphan_check(g: Guarded, schema: Schema, child: Table, fk: ForeignKey, sample: int, seconds: float) -> tuple[int, int, list[str], str]:
    """Count child rows (in a sample) whose FK value has no parent row.

    Returns (orphans, checked, example_values, note). Independent of PRAGMA foreign_keys.
    """
    pcols = parent_columns(schema, fk)
    parent = next((p for k, p in schema.tables.items() if k.lower() == fk.table.lower()), None)
    if parent is None:
        return 0, 0, [], f"parent table {fk.table} does not exist"
    if pcols is None:
        return 0, 0, [], f"cannot resolve the parent key of {fk.table}"
    src, how = sample_source(g, child, sample)
    nn = " AND ".join(f"c.{qi(c)} IS NOT NULL" for c in fk.from_cols)
    match = " AND ".join(f"p.{qi(pc)} = c.{qi(fc)}" for pc, fc in zip(pcols, fk.from_cols, strict=True))
    base = f"FROM {src} AS c WHERE {nn} AND NOT EXISTS (SELECT 1 FROM {qi(parent.name)} AS p WHERE {match})"
    total = g.scalar(f"SELECT count(*) FROM {src} AS c WHERE {nn}", seconds=seconds)
    orphans = g.scalar(f"SELECT count(*) {base}", seconds=seconds)
    ex = g.fetch(f"SELECT DISTINCT {', '.join('c.' + qi(c) for c in fk.from_cols)} {base} LIMIT 3", seconds=seconds).rows
    return orphans, total, [", ".join(fmt_cell(v, 30) for v in r) for r in ex], how


def _mermaid(schema: Schema) -> str:
    def nm(s: str) -> str:
        return '"' + s.replace('"', "'") + '"'

    out = ["erDiagram"]
    seen: set[str] = set()
    for t, fk in fk_edges(schema):
        nullable = not all(c.notnull or c.pk for c in t.columns if c.name in fk.from_cols)
        out.append(f"    {nm(fk.table)} {'|o' if nullable else '||'}--o{{ {nm(t.name)} : {nm(', '.join(fk.from_cols))}")
        seen.update((t.name, fk.table))
    for t in schema.tables.values():
        if t.kind == "table" and t.name not in seen and not t.shadow_of:
            out.append(f"    {nm(t.name)}")
    return "\n".join(out)


def _suggest_index(t: Table, cols: list[str]) -> str:
    name = "idx_" + re.sub(r"\W+", "_", t.name + "_" + "_".join(cols)).strip("_")
    return f"CREATE INDEX {qi(name)} ON {qi(t.name)} ({', '.join(qi(c) for c in cols)});"


def _load_order(schema: Schema) -> tuple[list[str], list[str]]:
    names = [t.name for t in schema.tables.values() if t.kind == "table" and not t.shadow_of]
    low = {n.lower(): n for n in names}
    deps: dict[str, set[str]] = {n: set() for n in names}
    for t, fk in fk_edges(schema):
        p = low.get(fk.table.lower())
        if p and p != t.name and t.name in deps:
            deps[t.name].add(p)
    order: list[str] = []
    remaining = dict(deps)
    while True:
        ready = sorted(n for n, d in remaining.items() if d <= set(order))
        if not ready:
            break
        order += ready
        for n in ready:
            del remaining[n]
    return order, sorted(remaining)


def schema_graph(g: Guarded, fmt: str, orphan_sample: int) -> str:
    schema = load_schema(g)
    edges = fk_edges(schema)
    tables = [t for t in schema.tables.values() if t.kind == "table" and not t.shadow_of]
    lines: list[str] = []
    if fmt == "mermaid":
        lines += ["```mermaid", _mermaid(schema), "```", ""]
    else:
        if not edges:
            lines.append("No foreign keys are declared in this database.")
        else:
            lines.append(f"Foreign keys ({len(edges)}), child -> parent:")
            for t, fk in edges:
                lines.append(f"  {t.name} {_fk_text(fk)}")
        involved = {t.name for t, _ in edges} | {fk.table for _, fk in edges}
        alone = [t.name for t in tables if t.name not in involved]
        if alone and edges:
            lines.append(f"\nTables with no declared relationships: {', '.join(alone)}")
        order, cyc = _load_order(schema)
        if edges:
            lines.append(f"\nLoad order (parents first): {' -> '.join(order)}")
            if cyc:
                lines.append(f"Circular references among: {', '.join(cyc)}")
        lines.append("")

    # missing indexes on FK columns
    missing = [(t, fk) for t, fk in edges if not fk_index_covered(t, fk.from_cols)]
    lines.append("Missing indexes on foreign-key columns:")
    if not edges:
        lines.append("  (no foreign keys)")
    elif not missing:
        lines.append("  none: every foreign key is covered by an index or the primary key.")
    else:
        lines.append("  Deleting or updating a parent row, or joining parent to child, scans the whole child table without these:")
        for t, fk in missing:
            lines.append(f"  {t.name}({', '.join(fk.from_cols)}) has no index. Suggestion: {_suggest_index(t, fk.from_cols)}")

    # orphans
    lines.append("")
    lines.append(f"Orphan check (child rows whose parent is missing; up to {orphan_sample} child rows sampled per key; independent of PRAGMA foreign_keys):")
    if not edges:
        lines.append("  (no foreign keys)")
    for t, fk in edges:
        label = f"  {t.name}({', '.join(fk.from_cols)}) -> {fk.table}"
        try:
            orphans, total, examples, note = orphan_check(g, schema, t, fk, orphan_sample, 10.0)
        except SqlScopeError as e:
            lines.append(f"{label}: check failed: {e}")
            continue
        if note.startswith(("parent table", "cannot resolve")):
            lines.append(f"{label}: {note}")
        elif orphans:
            lines.append(f"{label}: {orphans} of {total} non-null values have no parent ({orphans / total:.1%}); e.g. {'; '.join(examples)}  [checked {note}]")
        else:
            lines.append(f"{label}: ok ({total} non-null values checked, {note})")
    return "\n".join(lines)


# ---------------------------------------------------------------- schema_docs


def schema_docs(g: Guarded) -> str:
    schema = load_schema(g)
    stats = stat1_counts(g)
    name = g.path.name
    out = [f"# Database schema: {name}", ""]
    tables = [t for t in schema.tables.values() if t.kind == "table" and not t.shadow_of]
    views = [t for t in schema.tables.values() if t.kind == "view"]
    out.append(f"{len(tables)} tables, {len(views)} views, {schema.index_count} indexes, {len(schema.triggers)} triggers. File size {human_size(file_size(g.path))}.")
    out.append("")
    if any(t.foreign_keys for t in tables):
        out += ["## Relationships", "", "```mermaid", _mermaid(schema), "```", ""]
    out += ["## Tables", ""]
    for t in tables:
        out.append(f"### {t.name}")
        out.append("")
        kind = f"Virtual table using `{t.module}`." if t.module else "Table"
        extras = [x for x, on in (("WITHOUT ROWID", t.without_rowid), ("STRICT", t.strict)) if on]
        out.append(f"{kind}{' (' + ', '.join(extras) + ')' if extras else ''}. Rows: {count_label(g, t, stats)}.")
        out.append("")
        if t.error:
            out += [f"Not introspectable: {t.error}", "", "```sql", t.sql, "```", ""]
            continue
        refs = {c: fk for fk in t.foreign_keys for c in fk.from_cols}
        rows = []
        for c in t.data_columns:
            notes = []
            if c.pk:
                notes.append("primary key")
            if c.notnull:
                notes.append("not null")
            if c.name in refs:
                fk = refs[c.name]
                idx = fk.from_cols.index(c.name)
                notes.append(f"references {fk.table}({fk.to_cols[idx] or 'pk'})")
            if c.hidden in (2, 3):
                notes.append("generated")
            rows.append((c.name, c.type or "", "" if c.default is None else c.default, "; ".join(notes)))
        out.append(markdown_table(["column", "type", "default", "notes"], rows, 80))
        out.append("")
        if t.indexes:
            out.append("Indexes: " + "; ".join(f"`{i.name}` ({', '.join(c or '<expr>' for c in i.columns)}){' unique' if i.unique else ''}" for i in t.indexes))
            out.append("")
        checks = check_constraints(t.sql) if not t.module else []
        if checks:
            out.append("Checks: " + "; ".join(f"`{c}`" for c in checks))
            out.append("")
        if t.triggers:
            out.append("Triggers: " + ", ".join(f"`{n}`" for n, _ in t.triggers))
            out.append("")
    if views:
        out += ["## Views", ""]
        for v in views:
            out += [f"### {v.name}", "", "```sql", v.sql, "```", ""]
    if schema.triggers:
        out += ["## Triggers", ""]
        for n, tbl, sql in schema.triggers:
            out += [f"### {n} (on {tbl})", "", "```sql", sql, "```", ""]
    return "\n".join(out).rstrip() + "\n"
