"""A 1,000,000-row table: caps and time limits must hold on a realistically large file."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from conftest import tree_state

from sqlscope_mcp import server
from sqlscope_mcp.db import SqlScopeError

ROWS = 1_000_000


@pytest.fixture(scope="module")
def big(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("big")
    p = d / "big.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, user_id INTEGER, kind TEXT, payload TEXT, at TEXT)")
    c.execute(
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r WHERE n < ?) "
        "INSERT INTO events SELECT n, n % 5000, CASE n % 3 WHEN 0 THEN 'click' WHEN 1 THEN 'view' ELSE 'buy' END, "
        "'payload-' || n, '2024-01-' || printf('%02d', n % 28 + 1) FROM r",
        (ROWS,),
    )
    c.commit()
    c.close()
    return p


def test_million_rows_overview_is_fast_and_capped(big: Path) -> None:
    t = time.monotonic()
    out = server.db_overview(str(big))
    assert ">=100,000" in out  # exact counting stops at the cap
    assert time.monotonic() - t < 15


def test_select_star_returns_instantly_with_cap(big: Path) -> None:
    before = tree_state(big.parent)
    t = time.monotonic()
    out = server.query(str(big), "SELECT * FROM events")
    assert time.monotonic() - t < 10
    assert "200 row(s)" in out and "TRUNCATED" in out
    out = server.query(str(big), "SELECT * FROM events", max_rows=5000, format="csv")
    assert out.count("\n") < 5100 and "TRUNCATED" in out
    assert tree_state(big.parent) == before


def test_aggregate_over_all_rows(big: Path) -> None:
    out = server.query(str(big), "SELECT kind, count(*) AS n FROM events GROUP BY kind ORDER BY kind", timeout_seconds=30)
    assert "333333" in out or "333334" in out


def test_slow_query_times_out_cleanly(big: Path) -> None:
    t = time.monotonic()
    with pytest.raises(SqlScopeError, match="interrupted"):
        server.query(str(big), "SELECT count(*) FROM events a JOIN events b ON a.payload = b.payload || 'x' JOIN events c ON c.kind = a.kind", timeout_seconds=1)
    assert time.monotonic() - t < 30


def test_describe_data_quality_and_graph_on_big_table(big: Path) -> None:
    t = time.monotonic()
    out = server.describe_table(str(big), "events", stats_sample=10_000)
    assert "a sample of about 10000 rows" in out
    dq = server.data_quality(str(big), sample=10_000)
    assert "events" in dq
    server.schema_graph(str(big))
    assert time.monotonic() - t < 60


def test_find_in_db_budget_on_big_table(big: Path) -> None:
    t = time.monotonic()
    out = server.find_in_db(str(big), "no-such-needle-anywhere", seconds_per_table=0.5)
    assert time.monotonic() - t < 15
    assert "No matches" in out
    hit = server.find_in_db(str(big), "payload-999999", max_matches_per_table=3, seconds_per_table=10)
    assert "rowid=999999" in hit


def test_explain_flags_scan_with_row_count_cap(big: Path) -> None:
    out = server.explain_query(str(big), "SELECT * FROM events WHERE user_id = 7")
    assert "FULL TABLE SCAN of events (>=100,000 rows)" in out
    assert 'CREATE INDEX "idx_events_user_id"' in out


def test_find_in_db_reports_an_exhausted_budget(big: Path) -> None:
    from sqlscope_mcp import analyze
    from sqlscope_mcp.db import connect

    with connect(big) as g:
        out = analyze.find_in_db(g, "no-such-needle-anywhere", False, 3, 0.005)
    assert "Time budget (0.005 s per table) ran out in: events" in out
