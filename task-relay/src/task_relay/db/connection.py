"""SQLite connection helpers for write ordering, detailed-design §1."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def fetch_one(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def fetch_all(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
