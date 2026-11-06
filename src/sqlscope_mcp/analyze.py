"""explain_query, find_in_db and data_quality."""

from __future__ import annotations

import re
import time
from collections import Counter

from .db import Guarded, SqlScopeError, ensure_select, fmt_cell, qi
from .explore import COUNT_CAP, _n, _suggest_index, count_label, fk_edges, orphan_check, stat1_counts
from .schema import Schema, Table, fk_index_covered, load_schema, sample_source

# ---------------------------------------------------------------- explain_query

_ALIAS = re.compile(r'\b(?:FROM|JOIN)\s+("(?:[^"]|"")+"|\[[^\]]+\]|`[^`]+`|[\w$]+)(?:\s+(?:AS\s+)?(?!(?:WHERE|JOIN|LEFT|RIGHT|INNER|CROSS|NATURAL|ON|USING|GROUP|ORDER|LIMIT|UNION|EXCEPT|INTERSECT|HAVING|WINDOW|FULL|OUTER)\b)("(?:[^"]|"")+"|[\w$]+))?', re.I)
_PRED = re.compile(r'(?:("(?:[^"]|"")+"|[\w$]+)\s*\.\s*)?("(?:[^"]|"")+"|[A-Za-z_][\w$]*)\s*(==|=|<=|>=|<>|!=|<|>|\bIN\b|\bBETWEEN\b|\bLIKE\b|\bIS\b)', re.I)


def _unq(s: str) -> str:
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('""', '"')
    if len(s) >= 2 and s[0] == "[" and s[-1] == "]":
        return s[1:-1]
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        return s[1:-1]
    return s


def _aliases(sql: str, schema: Schema) -> dict[str, str]:
    """alias (lowercase) -> real table name."""
    low = {k.lower(): k for k in schema.tables}
    out: dict[str, str] = {}
    for m in _ALIAS.finditer(sql):
        real = low.get(_unq(m.group(1)).lower())
        if real:
            out[_unq(m.group(1)).lower()] = real
            if m.group(2):
                out[_unq(m.group(2)).lower()] = real
    return out


def _predicate_columns(sql: str, table: Table, aliases: dict[str, str]) -> tuple[list[str], list[str]]:
    """Columns of ``table`` used in comparisons (equality-like, range-like). Heuristic."""
    where = re.split(r"\bWHERE\b", sql, maxsplit=1, flags=re.I)
    text = sql if len(where) == 1 else sql
    cols = {c.name.lower(): c.name for c in table.columns}
    eq: list[str] = []
    rng: list[str] = []
    for m in _PRED.finditer(text):
        qual, col, op = m.group(1), _unq(m.group(2)), m.group(3).upper()
        if col.lower() not in cols:
            continue
        if qual is not None and aliases.get(_unq(qual).lower()) != table.name:
            continue
        name = cols[col.lower()]
        bucket = eq if op in ("=", "==", "IN", "IS") else rng
        if name not in eq and name not in rng:
            bucket.append(name)
    return eq, rng


def explain_query(g: Guarded, sql: str, schema: Schema | None = None) -> str:
    ensure_select(sql)
    schema = schema or load_schema(g)
    plan = g.fetch("EXPLAIN QUERY PLAN " + sql, max_rows=2000).rows
    if not plan:
        return "SQLite returned an empty plan."
    children: dict[int, list[tuple[int, str]]] = {}
    for rid, parent, _, detail in plan:
        children.setdefault(parent, []).append((rid, detail))
    lines = ["Query plan:"]

    def walk(parent: int, depth: int) -> None:
        for rid, detail in children.get(parent, []):
            lines.append("  " * (depth + 1) + "- " + detail)
            walk(rid, depth + 1)

    walk(0, 0)
    aliases = _aliases(sql, schema)
    stats = stat1_counts(g)
    findings: list[str] = []
    suggestions: list[str] = []
    seen_scan: set[str] = set()
    for _, _, _, detail in plan:
        m = re.match(r"SCAN (?:TABLE )?(\S+)(?: AS \S+)?(.*)$", detail)
        if m and "VIRTUAL TABLE INDEX" not in detail and "CONSTANT ROW" not in detail:
            target = m.group(1)
            real = aliases.get(_unq(target).lower()) or next((k for k in schema.tables if k.lower() == _unq(target).lower()), None)
            if real is None:
                continue  # a subquery or CTE
            t = schema.tables[real]
            size = count_label(g, t, stats, cap=COUNT_CAP)
            rest = m.group(2)
            if "COVERING INDEX" in rest:
                findings.append(f"FULL INDEX SCAN of {real} ({size} rows) {rest.strip()}: reads every index entry, but not the table.")
            elif "USING INDEX" in rest:
                findings.append(f"FULL SCAN of {real} ({size} rows) walking {rest.replace('USING INDEX', '').strip()} to avoid a sort: still reads every row.")
            elif t.kind == "view":
                continue
            else:
                findings.append(f"FULL TABLE SCAN of {real} ({size} rows): every row is read.")
            if real not in seen_scan and t.kind == "table":
                seen_scan.add(real)
                eq, rng = _predicate_columns(sql, t, aliases)
                cols = (eq + rng[:1])
                if cols and not fk_index_covered(t, [cols[0]]) and not (t.is_rowid_alias_pk and cols[0] in t.pk_columns):
                    suggestions.append(f"{real} is scanned and the query filters on {', '.join(cols)}: {_suggest_index(t, cols)}  (heuristic: check selectivity first)")
        if "USE TEMP B-TREE" in detail:
            what = detail.split("FOR", 1)[-1].strip()
            findings.append(f"TEMP B-TREE for {what}: rows are sorted/deduplicated in a temporary structure; an index on those columns in that order avoids it.")
        am = re.match(r"SEARCH (\S+) USING AUTOMATIC (?:COVERING )?INDEX \((.*)\)", detail)
        if am:
            real = aliases.get(_unq(am.group(1)).lower()) or am.group(1)
            cols = [c.split("=")[0].split(">")[0].split("<")[0].strip() for c in am.group(2).split(" AND ")]
            tbl = schema.tables.get(real)
            findings.append(f"AUTOMATIC INDEX on {real}({', '.join(cols)}): SQLite builds a temporary index on every run because none exists.")
            if tbl is not None and tbl.kind == "table":
                suggestions.append(f"Create it once: {_suggest_index(tbl, cols)}")
        if "CORRELATED" in detail:
            findings.append(f"{detail}: re-evaluated once per outer row; make sure its inner lookup is a SEARCH, not a SCAN.")
    lines.append("")
    if findings:
        lines.append("Findings:")
        lines += [f"  * {f}" for f in dict.fromkeys(findings)]
    else:
        lines.append("Findings: no full table scans, temporary sort structures or automatic indexes.")
    if suggestions:
        lines.append("")
        lines.append("Index suggestions (text only, nothing was created):")
        lines += [f"  * {s}" for s in dict.fromkeys(suggestions)]
    lines.append("")
    lines.append("This is the plan only; the query was not run.")
    return "\n".join(lines)


# ---------------------------------------------------------------- find_in_db


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fold(s: str) -> str:
    """SQLite's LIKE folds only ASCII case; mirror that (and keep string length)."""
    return "".join(ch.lower() if ch.isascii() else ch for ch in s)


def _snippet(value: str, term: str, case_sensitive: bool, width: int = 40) -> str:
    hay = value if case_sensitive else _fold(value)
    needle = term if case_sensitive else _fold(term)
    i = hay.find(needle)
    if i < 0:
        i = 0
    start, end = max(0, i - width), min(len(value), i + len(term) + width)
    return ("..." if start else "") + value[start:end].replace("\n", " ") + ("..." if end < len(value) else "")


def find_in_db(g: Guarded, term: str, case_sensitive: bool, max_per_table: int, seconds_per_table: float, total_seconds: float = 60.0) -> str:
    if term == "":
        raise SqlScopeError("term is empty.")
    schema = load_schema(g)
    started = time.monotonic()
    out: list[str] = []
    skipped: list[str] = []
    timed_out: list[str] = []
    unsearched: list[str] = []
    searched = 0
    hits = 0
    for t in schema.tables.values():
        if t.kind != "table" or t.module or t.shadow_of or t.error:
            if t.kind == "table":
                skipped.append(t.name + (" (virtual)" if t.module else " (shadow)" if t.shadow_of else ""))
            continue
        cols = t.data_columns
        if not cols:
            continue
        if time.monotonic() - started > total_seconds:
            unsearched.append(t.name)
            continue
        if case_sensitive:
            conds = [f"(typeof({qi(c.name)}) <> 'blob' AND instr(CAST({qi(c.name)} AS TEXT), ?) > 0)" for c in cols]
            params = [term] * len(cols)
        else:
            conds = [f"(typeof({qi(c.name)}) <> 'blob' AND CAST({qi(c.name)} AS TEXT) LIKE ? ESCAPE '\\')" for c in cols]
            params = [f"%{_like_escape(term)}%"] * len(cols)
        ident = "rowid, " if t.has_rowid else ""
        sel = ident + ", ".join(qi(c.name) for c in cols)
        sql = f"SELECT {sel} FROM {qi(t.name)} WHERE {' OR '.join(conds)} LIMIT {int(max_per_table)}"
        r = g.fetch(sql, params, max_rows=max_per_table, seconds=seconds_per_table, partial_ok=True)
        searched += 1
        if r.timed_out:
            timed_out.append(t.name)
        if not r.rows:
            continue
        offset = 1 if t.has_rowid else 0
        pk_idx = [i for i, c in enumerate(cols) if c.pk]
        out.append(f"{t.name}: {len(r.rows)}{'+' if len(r.rows) >= max_per_table else ''} matching row(s)")
        for row in r.rows:
            if t.has_rowid:
                where = f"rowid={row[0]}"
            elif pk_idx:
                where = "pk=(" + ", ".join(fmt_cell(row[offset + i], 30) for i in pk_idx) + ")"
            else:
                where = "row"
            matched = []
            for i, c in enumerate(cols):
                v = row[offset + i]
                if v is None or isinstance(v, bytes):
                    continue
                text = str(v)
                if (term in text) if case_sensitive else (_fold(term) in _fold(text)):
                    matched.append(f"{c.name} = {_snippet(text, term, case_sensitive)!r}")
            out.append(f"  {where}: " + "; ".join(matched or ["(match is in a column value not shown here)"]))
            hits += 1
    head = [f"Searched {searched} table(s) for {term!r} ({'case-sensitive' if case_sensitive else 'case-insensitive for ASCII letters'}, substring match) in all non-BLOB columns; {hits} row(s) shown."]
    if not out:
        head.append("No matches.")
    tail = []
    if timed_out:
        tail.append(f"Time budget ({seconds_per_table:g} s per table) ran out in: {', '.join(timed_out)}; those results may be incomplete.")
    if unsearched:
        tail.append(f"Total time budget ({total_seconds:g} s) ran out before: {', '.join(unsearched)}.")
    if skipped:
        tail.append(f"Not searched: {', '.join(skipped)}.")
    return "\n".join(head + out + tail)


# ---------------------------------------------------------------- data_quality

_KEYISH = re.compile(r"^(id|uuid|guid|key|code|number|no|email|e_?mail|slug|sku|isbn|username|login|handle|iban)$|_(uuid|guid|email|slug|sku|isbn|username|iban)$", re.I)
_DATE_LIKE = re.compile(r"^\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})([ T]\d{1,2}:\d{2}.*)?\s*$")
_ISO = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")
_NUMERIC_TYPES = {"integer", "real"}


def _signature(v: str) -> str:
    s = v.strip()
    s = re.sub(r"(?<=:\d\d)\.\d+", ".f", s)
    s = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "TZ", s)
    return re.sub(r"\d", "9", s)


def _is_textish(decl: str) -> bool:
    d = decl.upper()
    return d == "" or any(k in d for k in ("CHAR", "CLOB", "TEXT", "DATE", "TIME"))


def quality_for_table(g: Guarded, schema: Schema, t: Table, sample: int, seconds: float) -> list[tuple[str, str]]:
    """Findings for one table as (severity, text); severity is WARN or INFO."""
    f: list[tuple[str, str]] = []
    cols = t.data_columns
    if not cols:
        return f
    if t.name == "":
        return f
    src, how = sample_source(g, t, sample)
    total = g.scalar(f"SELECT count(*) FROM {src}", seconds=seconds)
    if total == 0:
        return [("INFO", "the table is empty")]
    capped = cols[:60]
    if len(cols) > 60:
        f.append(("INFO", f"only the first 60 of {len(cols)} columns were checked"))

    # --- no key
    if t.kind == "table" and not t.pk_columns and not any(i.unique for i in t.indexes) and not t.without_rowid:
        f.append(("INFO", "no PRIMARY KEY or UNIQUE constraint: nothing stops duplicate rows"))

    # --- NULL / empty
    aggs = ", ".join(f"sum({qi(c.name)} IS NULL), sum({qi(c.name)} = '')" for c in capped)
    r = g.fetch(f"SELECT {aggs} FROM {src}", seconds=seconds).rows[0]
    for i, c in enumerate(capped):
        nulls, empty = r[2 * i] or 0, r[2 * i + 1] or 0
        if nulls == total:
            f.append(("WARN", f"{c.name}: always NULL ({total} rows checked); an unused column?"))
        elif nulls / total >= 0.5:
            f.append(("WARN", f"{c.name}: {nulls / total:.0%} NULL ({nulls} of {total})"))
        if empty and nulls:
            f.append(("WARN", f"{c.name}: both NULL ({nulls}) and empty string ({empty}) mean 'no value'; consumers must handle both"))
        elif empty and empty / total >= 0.5:
            f.append(("INFO", f"{c.name}: {empty / total:.0%} empty strings"))

    # --- mixed storage types
    for c in capped:
        dist = g.fetch(f"SELECT typeof({qi(c.name)}), count(*) FROM {src} WHERE {qi(c.name)} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC", seconds=seconds).rows
        kinds = {k for k, _ in dist}
        if len(kinds) > 1:
            txt = ", ".join(f"{k} {n}" for k, n in dist)
            if kinds <= _NUMERIC_TYPES:
                f.append(("INFO", f"{c.name} (declared {c.type or 'no type'}): integers and reals mixed ({txt})"))
                continue
            decl = c.type.upper()
            ex = g.fetch(f"SELECT {qi(c.name)}, typeof({qi(c.name)}) FROM {src} WHERE typeof({qi(c.name)}) <> ? AND {qi(c.name)} IS NOT NULL LIMIT 2", (dist[0][0],), seconds=seconds).rows
            examples = "; ".join(f"{fmt_cell(v, 30)!r} ({k})" for v, k in ex)
            conflict = " - the declared affinity is numeric, so these text values could not be converted" if ("INT" in decl or "REAL" in decl or "FLOA" in decl or "DOUB" in decl or "NUM" in decl) and "text" in kinds else ""
            f.append(("WARN", f"{c.name} (declared {c.type or 'no type'}): mixed storage types: {txt}{conflict}. Minority examples: {examples}"))

    # --- duplicates
    fk_cols = {c.lower() for fk in t.foreign_keys for c in fk.from_cols}
    unique_single = {i.columns[0].lower() for i in t.indexes if i.unique and len(i.columns) == 1 and i.columns[0]}
    for c in capped:
        if not _KEYISH.search(c.name) or c.name.lower() in fk_cols or c.name.lower() in unique_single:
            continue
        if c.pk and len(t.pk_columns) == 1:
            continue
        q = qi(c.name)
        d = g.fetch(f"SELECT count(*), sum(n) - count(*) FROM (SELECT count(*) AS n FROM {src} WHERE {q} IS NOT NULL GROUP BY {q} HAVING n > 1)", seconds=seconds).rows[0]
        if d[0]:
            ex = g.fetch(f"SELECT {q}, count(*) FROM {src} WHERE {q} IS NOT NULL GROUP BY {q} HAVING count(*) > 1 ORDER BY 2 DESC LIMIT 3", seconds=seconds).rows
            f.append(("WARN", f"{c.name} looks like a key but is not unique: {d[0]} value(s) repeat ({d[1]} extra rows), e.g. " + ", ".join(f"{fmt_cell(v, 30)!r} x{n}" for v, n in ex)))
        if any(k in c.name.lower() for k in ("email", "mail", "username", "login", "handle", "slug")):
            d2 = g.fetch(f"SELECT count(*), sum(n) - count(*) FROM (SELECT count(*) AS n FROM {src} WHERE {q} IS NOT NULL GROUP BY lower(trim({q})) HAVING n > 1)", seconds=seconds).rows[0]
            if d2[0] > d[0]:
                f.append(("WARN", f"{c.name}: {d2[0]} value(s) collide after lower() and trim() ({d2[1]} extra rows; {d[1] or 0} are exact duplicates)"))
    if t.kind == "table" and 0 < len(cols) <= 30 and not t.module:
        non_pk = [c for c in cols if not c.pk and c.hidden not in (2, 3)]
        if non_pk and (t.is_rowid_alias_pk or not t.pk_columns):
            grp = ", ".join(qi(c.name) for c in non_pk)
            d = g.fetch(f"SELECT count(*), sum(n) - count(*) FROM (SELECT count(*) AS n FROM {src} GROUP BY {grp} HAVING n > 1)", seconds=seconds).rows[0]
            if d[0]:
                f.append(("WARN", f"{d[0]} group(s) of rows are identical in every non-key column ({d[1]} redundant rows)"))

    # --- date-looking text with inconsistent formats
    for c in capped:
        if not _is_textish(c.type):
            continue
        q = qi(c.name)
        vals = [v for (v,) in g.fetch(f"SELECT {q} FROM {src} WHERE typeof({q}) = 'text' AND {q} <> '' LIMIT 500", max_rows=500, seconds=seconds).rows]
        if len(vals) < 3:
            continue
        datey = sum(1 for v in vals if _DATE_LIKE.match(v))
        if datey / len(vals) < 0.6:
            continue
        sigs = Counter(_signature(v) for v in vals)
        msgs = []
        if len(sigs) > 1:
            top = ", ".join(f"{s!r} x{n}" for s, n in sigs.most_common(4))
            msgs.append(f"{len(sigs)} different formats: {top}")
        if datey < len(vals):
            bad = [v for v in vals if not _DATE_LIKE.match(v)]
            msgs.append(f"{len(bad)} value(s) are not date-shaped, e.g. {fmt_cell(bad[0], 30)!r}")
        invalid = 0
        for v in vals:
            m = _ISO.match(v)
            if m and not (1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31):
                invalid += 1
        if invalid:
            msgs.append(f"{invalid} ISO-style value(s) with an impossible month or day")
        slash = [re.match(r"^\s*(\d{1,2})[./](\d{1,2})[./]\d{2,4}", v) for v in vals]
        firsts = [int(m.group(1)) for m in slash if m]
        seconds_ = [int(m.group(2)) for m in slash if m]
        if firsts and any(a > 12 for a in firsts) and any(b > 12 for b in seconds_):
            msgs.append("day-month order is mixed (some values only make sense as DD/MM, others as MM/DD)")
        if msgs:
            f.append(("WARN", f"{c.name}: date-looking text ({datey} of {len(vals)} sampled values) is inconsistent: " + "; ".join(msgs)))
        else:
            f.append(("INFO", f"{c.name}: date-looking text, one consistent format {next(iter(sigs))!r} (stored as text; SQLite has no date type)"))

    f.insert(0, ("INFO", f"checked {how}"))
    return f


def data_quality(g: Guarded, table: str | None, sample: int, total_seconds: float = 60.0) -> str:
    schema = load_schema(g)
    targets = [schema.find(table)] if table else [t for t in schema.tables.values() if t.kind == "table" and not t.module and not t.shadow_of and not t.error]
    started = time.monotonic()
    blocks: list[str] = []
    warn = 0
    skipped: list[str] = []
    for t in targets:
        if time.monotonic() - started > total_seconds:
            skipped.append(t.name)
            continue
        try:
            found = quality_for_table(g, schema, t, sample, seconds=20.0)
        except SqlScopeError as e:
            blocks.append(f"## {t.name}\n  check failed: {e}")
            continue
        warn += sum(1 for s, _ in found if s == "WARN")
        order = {"WARN": 0, "INFO": 1}
        body = [f"  [{s}] {m}" for s, m in sorted(found, key=lambda x: order[x[0]]) if m]
        blocks.append(f"## {t.name}\n" + "\n".join(body))

    fk_lines: list[str] = []
    for child, fk in fk_edges(schema):
        if table and child.name != schema.find(table).name:
            continue
        if time.monotonic() - started > total_seconds:
            break
        label = f"{child.name}({', '.join(fk.from_cols)}) -> {fk.table}"
        try:
            orphans, checked, examples, note = orphan_check(g, schema, child, fk, sample, 20.0)
        except SqlScopeError as e:
            fk_lines.append(f"  [INFO] {label}: check failed: {e}")
            continue
        if note.startswith(("parent table", "cannot resolve")):
            fk_lines.append(f"  [WARN] {label}: {note}")
            warn += 1
        elif orphans:
            warn += 1
            fk_lines.append(f"  [WARN] {label}: {orphans} of {checked} non-null values have no parent row; e.g. {'; '.join(examples)} (checked {note})")
    head = [f"Data quality report: {len(targets)} table(s), {warn} warning(s). Findings come from SQL over a sample (up to {_n(sample)} rows per table) and are independent of PRAGMA foreign_keys.", ""]
    if fk_lines:
        head += ["## Dangling foreign keys (foreign_keys enforcement does not matter here)"] + fk_lines + [""]
    elif fk_edges(schema):
        head += ["Foreign keys: no dangling references found in the sample.", ""]
    tail = []
    if skipped:
        tail.append(f"Total time budget ran out before: {', '.join(skipped)}. Call again with table=NAME.")
    return "\n".join(head + ["\n".join(blocks)] + tail)
