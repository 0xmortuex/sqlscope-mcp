"""Shared fixtures: real SQLite databases built with the stdlib sqlite3 module."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from sqlscope_mcp import server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_sandbox() -> None:
    server.configure(None)


def has_fts5() -> bool:
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def has_strict() -> bool:
    return sqlite3.sqlite_version_info >= (3, 37, 0)


def tree_state(folder: Path) -> dict[str, str]:
    """name -> sha256 of every file in ``folder`` (not recursive)."""
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(folder.iterdir()) if f.is_file()}


def build_shop(path: Path) -> None:
    """A small shop database with deliberate problems for the data-quality checks."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            name TEXT,
            city TEXT,
            created TEXT,
            notes TEXT
        );
        CREATE TABLE products (
            sku TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            price REAL CHECK (price >= 0 AND price < 1000000),
            qty INTEGER NOT NULL DEFAULT 0 CHECK (qty >= 0),
            gross REAL GENERATED ALWAYS AS (price * 1.2) VIRTUAL,
            thumb BLOB
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id) ON DELETE CASCADE,
            placed TEXT,
            total INTEGER
        );
        CREATE TABLE order_items (
            order_id INTEGER NOT NULL REFERENCES orders(id),
            sku TEXT NOT NULL REFERENCES products(sku),
            qty INTEGER NOT NULL,
            PRIMARY KEY (order_id, sku)
        ) WITHOUT ROWID;
        CREATE TABLE audit (what TEXT, at TEXT);
        CREATE INDEX idx_orders_placed ON orders(placed);
        CREATE VIEW big_orders AS SELECT id, total FROM orders WHERE total > 100;
        CREATE TRIGGER trg_audit AFTER INSERT ON orders BEGIN
            INSERT INTO audit VALUES ('order ' || new.id, 'now');
        END;
        CREATE TABLE "müşteriler" ("şehir" TEXT, "ad soyad" TEXT);
        CREATE TABLE "we""ird" ("co""l" TEXT, "select" INTEGER);
        """
    )
    # customers: 2 duplicate emails (one only after lower/trim), mixed date formats, NULL-heavy notes
    rows = []
    for i in range(1, 201):
        email = f"user{i}@example.com"
        created = f"2024-03-{(i % 28) + 1:02d}"
        rows.append((i, email, f"Name {i}", "Istanbul", created, None))
    rows[10] = (11, "user5@example.com", "Dup", "Ankara", "2024-03-12", None)  # exact dup of user5
    rows[20] = (21, " User6@Example.com ", "Dup2", "Izmir", "12.03.2024", "x")  # collides after lower/trim
    rows[30] = (31, "user31@example.com", "Mixed", "İstanbul", "03/12/2024", None)
    rows[40] = (41, "user41@example.com", "Mixed2", "Bursa", "2024-03-12T10:20:30Z", None)
    rows[50] = (51, "user51@example.com", "Mixed3", "Bursa", "2024-13-45", None)
    con.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", rows)
    con.executemany(
        "INSERT INTO products(sku, title, price, qty, thumb) VALUES (?,?,?,?,?)",
        [("A1", "Widget", 9.5, 10, b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4), ("B2", "Gadget", 20, 5, None), ("C3", "Gizmo", 3.25, 0, None)],
    )
    orders = [(i, (i % 200) + 1, f"2024-04-{(i % 28) + 1:02d}", i * 10) for i in range(1, 301)]
    orders += [(1001, 99999, "2024-05-01", 5), (1002, 88888, "2024-05-02", "n/a")]  # orphans, mixed total type
    con.executemany("INSERT INTO orders VALUES (?,?,?,?)", orders)
    con.executemany("INSERT INTO order_items VALUES (?,?,?)", [(1, "A1", 2), (1, "B2", 1), (2, "A1", 1), (3, "ZZZ", 1)])
    con.executemany(
        'INSERT INTO "müşteriler" VALUES (?,?)',
        [("İstanbul", "Çağrı Şahin"), ("Ankara", "Ayşe Yılmaz"), ("istanbul", "ışık")],
    )
    con.execute('INSERT INTO "we""ird" VALUES (?, ?)', ('he said "hi"', 1))
    con.execute("UPDATE customers SET notes = ? WHERE id = 1", ("L" * 5000,))
    con.commit()
    con.close()


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    p = tmp_path / "shop.db"
    build_shop(p)
    return p
