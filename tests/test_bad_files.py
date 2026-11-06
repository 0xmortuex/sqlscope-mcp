"""Hostile and broken inputs: every one must produce a clear error, never a crash or a modified file."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sqlscope_mcp import server
from sqlscope_mcp.db import SqlScopeError

ALL_TOOLS = [
    lambda p: server.db_overview(p),
    lambda p: server.describe_table(p, "t"),
    lambda p: server.schema_graph(p),
    lambda p: server.query(p, "SELECT 1"),
    lambda p: server.explain_query(p, "SELECT 1"),
    lambda p: server.find_in_db(p, "x"),
    lambda p: server.data_quality(p),
    lambda p: server.schema_docs(p),
]


def test_zero_byte_file(tmp_path: Path) -> None:
    f = tmp_path / "empty.db"
    f.write_bytes(b"")
    for tool in ALL_TOOLS:
        with pytest.raises(SqlScopeError, match="empty .0 bytes."):
            tool(str(f))
    assert f.stat().st_size == 0 and os.listdir(tmp_path) == ["empty.db"]


def test_text_file_is_not_a_database(tmp_path: Path) -> None:
    f = tmp_path / "notes.db"
    f.write_text("hello, this is just a text file\n" * 10, encoding="utf-8")
    with pytest.raises(SqlScopeError, match="not an SQLite database"):
        server.db_overview(str(f))
    assert os.listdir(tmp_path) == ["notes.db"]


def test_encrypted_looking_file_says_so(tmp_path: Path) -> None:
    f = tmp_path / "cipher.db"
    f.write_bytes(bytes((i * 167 + 13) % 256 for i in range(8192)))  # high-entropy header, like SQLCipher
    with pytest.raises(SqlScopeError, match="encrypted .*SQLCipher"):
        server.db_overview(str(f))
    with pytest.raises(SqlScopeError, match="encrypted"):
        server.query(str(f), "SELECT 1")


def test_truncated_header(tmp_path: Path) -> None:
    f = tmp_path / "trunc.db"
    f.write_bytes(b"SQLite format 3\x00" + b"\x10\x00" + b"\x00" * 10)
    with pytest.raises(SqlScopeError, match="truncated"):
        server.db_overview(str(f))


def test_damaged_page_size(tmp_path: Path) -> None:
    f = tmp_path / "ps.db"
    f.write_bytes(b"SQLite format 3\x00" + b"\x00\x07" + b"\x00" * 200)
    with pytest.raises(SqlScopeError, match="invalid page size"):
        server.db_overview(str(f))


def test_corrupt_database_body(shop: Path, tmp_path: Path) -> None:
    data = bytearray(shop.read_bytes())
    bad = tmp_path / "corrupt.db"
    for i in range(100, 4096):
        data[i] = (i * 31) % 251
    bad.write_bytes(bytes(data))
    before = bad.read_bytes()
    with pytest.raises(SqlScopeError, match="corrupt|not a readable SQLite|malformed|SQLite error"):
        server.db_overview(str(bad))
    assert bad.read_bytes() == before


def test_missing_file_and_directory_hints(shop: Path) -> None:
    with pytest.raises(SqlScopeError, match=r"No such file: .*nope\.db\. SQLite-looking files in .*: shop\.db"):
        server.db_overview(str(shop.parent / "nope.db"))
    with pytest.raises(SqlScopeError, match=r"is a folder, not a database file\. SQLite-looking files in it: shop\.db"):
        server.db_overview(str(shop.parent))


def test_blank_and_nul_paths() -> None:
    with pytest.raises(SqlScopeError, match="path is empty"):
        server.db_overview("   ")
    with pytest.raises(SqlScopeError, match="NUL"):
        server.db_overview("a\x00b.db")


def test_unicode_path(tmp_path: Path) -> None:
    import sqlite3

    d = tmp_path / "veri tabanı #1 %20"
    d.mkdir()
    f = d / "şirket#1.db"
    c = sqlite3.connect(f)
    c.execute("CREATE TABLE t (a)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()
    before = os.listdir(d)
    assert "| t | table | 1 |" in server.db_overview(str(f))
    assert os.listdir(d) == before


def test_duckdb_like_header_is_rejected(tmp_path: Path) -> None:
    f = tmp_path / "x.db"
    f.write_bytes(b"\x00\x00\x00\x00DUCK" + b"\x01" * 200)
    with pytest.raises(SqlScopeError, match="not an SQLite database"):
        server.db_overview(str(f))
