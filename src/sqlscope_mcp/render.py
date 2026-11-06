"""Output shaping: markdown / CSV / JSON tables with cell and byte caps."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from typing import Any

from .db import fmt_cell

OUTPUT_LIMIT = 120_000  # characters returned by any tool


def clip(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [output cut at {limit} characters of {len(text)}]"


def _md(cell: str) -> str:
    cell = cell.replace("\\", "\\\\").replace("|", "\\|")
    return cell.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], max_chars: int = 200) -> str:
    out = ["| " + " | ".join(_md(h) for h in headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(_md(fmt_cell(v, max_chars)) for v in r) + " |")
    return "\n".join(out)


def csv_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], max_chars: int = 200) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(headers)
    for r in rows:
        w.writerow([fmt_cell(v, max_chars) for v in r])
    return buf.getvalue().rstrip("\n")


def _json_value(v: Any, max_chars: int) -> Any:
    if v is None or isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else str(v)
    return fmt_cell(v, max_chars)


def json_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], max_chars: int = 200) -> str:
    objs = []
    for r in rows:
        obj: dict[str, Any] = {}
        for i, (h, v) in enumerate(zip(headers, r, strict=True)):
            key = h if h not in obj else f"{h}_{i}"
            obj[key] = _json_value(v, max_chars)
        objs.append(obj)
    return json.dumps(objs, ensure_ascii=False, indent=1)


def table(fmt: str, headers: Sequence[str], rows: Sequence[Sequence[Any]], max_chars: int = 200) -> str:
    if fmt == "markdown":
        return markdown_table(headers, rows, max_chars)
    if fmt == "csv":
        return csv_table(headers, rows, max_chars)
    if fmt == "json":
        return json_table(headers, rows, max_chars)
    raise ValueError(f"unknown format {fmt!r}; use markdown, csv or json")


def cap_rows_by_bytes(rows: Sequence[Sequence[Any]], max_bytes: int, max_chars: int) -> tuple[int, bool]:
    """How many leading rows fit in roughly ``max_bytes`` of rendered text."""
    total = 0
    for i, r in enumerate(rows):
        total += sum(len(fmt_cell(v, max_chars)) + 3 for v in r)
        if total > max_bytes:
            return max(i, 1), True
    return len(rows), False
