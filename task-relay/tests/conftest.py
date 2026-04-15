import sqlite3

import pytest
from pathlib import Path
from collections.abc import Iterator


@pytest.fixture
def sqlite_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    from task_relay.db.connection import connect
    from task_relay.db.migrations import apply_schema

    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)
    yield conn
    conn.close()
