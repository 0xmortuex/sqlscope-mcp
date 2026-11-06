"""MCP stdio smoke test: spawns the real server over stdio, lists the tools, calls them, and
checks that instructive errors survive the trip to the agent (MCPServer hides any exception
that isn't a ToolError)."""

import os
import sys
from pathlib import Path

import pytest
from conftest import build_shop, tree_state

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

EXPECTED_TOOLS = {
    "db_overview",
    "describe_table",
    "schema_graph",
    "query",
    "explain_query",
    "find_in_db",
    "data_quality",
    "schema_docs",
}


def params(*extra: str) -> StdioServerParameters:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return StdioServerParameters(command=sys.executable, args=["-m", "sqlscope_mcp", *extra], env=env)


@pytest.mark.anyio
async def test_tools_and_a_session_over_stdio(tmp_path: Path) -> None:
    db = tmp_path / "shop.db"
    build_shop(db)
    before = tree_state(tmp_path)
    async with stdio_client(params()) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "sqlscope"
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == EXPECTED_TOOLS
        assert all(t.annotations and t.annotations.read_only_hint for t in tools)

        res = await session.call_tool("db_overview", {"path": str(db)})
        assert not res.is_error and "customers" in res.content[0].text and "müşteriler" in res.content[0].text
        res = await session.call_tool("query", {"path": str(db), "sql": "SELECT count(*) AS n FROM orders", "format": "json"})
        assert not res.is_error and '"n": 302' in res.content[0].text
        res = await session.call_tool("explain_query", {"path": str(db), "sql": "SELECT * FROM orders WHERE customer_id = 1"})
        assert "FULL TABLE SCAN" in res.content[0].text
        res = await session.call_tool("schema_graph", {"path": str(db), "format": "mermaid"})
        assert "erDiagram" in res.content[0].text
        for name, args in [("describe_table", {"table": "orders"}), ("find_in_db", {"term": "Istanbul"}), ("data_quality", {}), ("schema_docs", {})]:
            res = await session.call_tool(name, {"path": str(db), **args})
            assert not res.is_error and res.content[0].text, name
    assert tree_state(tmp_path) == before


@pytest.mark.anyio
async def test_instructive_errors_reach_the_agent(tmp_path: Path) -> None:
    db = tmp_path / "shop.db"
    build_shop(db)
    before = tree_state(tmp_path)
    async with stdio_client(params()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool("query", {"path": str(db), "sql": "DROP TABLE orders"})
        assert res.is_error and "Only SELECT / WITH / VALUES" in res.content[0].text and "read-only by construction" in res.content[0].text
        res = await session.call_tool("query", {"path": str(db), "sql": "SELECT 1; DROP TABLE orders"})
        assert res.is_error and "one statement" in res.content[0].text
        res = await session.call_tool("query", {"path": str(db), "sql": "SELECT load_extension('x.dll')"})
        assert res.is_error and "Denied" in res.content[0].text
        res = await session.call_tool("describe_table", {"path": str(db), "table": "orderz"})
        assert res.is_error and "Did you mean: orders" in res.content[0].text
        res = await session.call_tool("db_overview", {"path": str(tmp_path / "missing.db")})
        assert res.is_error and "No such file" in res.content[0].text and "shop.db" in res.content[0].text
        res = await session.call_tool("query", {"path": str(db), "sql": "SELECT 1", "max_rows": 0})
        assert res.is_error and "max_rows must be between" in res.content[0].text
    assert tree_state(tmp_path) == before


@pytest.mark.anyio
async def test_dir_sandbox_over_stdio(tmp_path: Path) -> None:
    box = tmp_path / "box"
    box.mkdir()
    build_shop(box / "in.db")
    build_shop(tmp_path / "out.db")
    async with stdio_client(params("--dir", str(box))) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool("db_overview", {"path": "in.db"})
        assert not res.is_error and "customers" in res.content[0].text
        for bad in ["../out.db", str(tmp_path / "out.db")]:
            res = await session.call_tool("db_overview", {"path": bad})
            assert res.is_error and "outside the allowed folder" in res.content[0].text


def test_bad_dir_argument_exits_with_a_message(tmp_path: Path) -> None:
    import subprocess

    r = subprocess.run(
        [sys.executable, "-m", "sqlscope_mcp", "--dir", str(tmp_path / "nope")],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )
    assert r.returncode != 0 and "is not a folder" in r.stderr
