from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import fakeredis

from task_relay.branch_lease.redis_lease import RedisLease
from task_relay.clock import FrozenClock
from task_relay.config import Settings
from task_relay.db.connection import connect
from task_relay.runner.unlock_handler import UnlockHandler
from task_relay.types import BranchWaiterStatus, JournalPosition

from tests.unit._test_helpers import seed_task


def test_handle_unlock_deletes_lease_requeues_head_and_keeps_last_token(sqlite_conn: sqlite3.Connection) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    redis_client = fakeredis.FakeStrictRedis(decode_responses=True)
    redis_lease = RedisLease(redis_client, Settings(), FrozenClock(now))
    journal_writer = Mock()
    journal_writer.append.return_value = JournalPosition(file="20260415.ndjson.zst", offset=0)
    seed_task(sqlite_conn, task_id="task-head", created_at=now)
    seed_task(sqlite_conn, task_id="task-next", created_at=now)
    sqlite_conn.execute(
        "INSERT INTO branch_waiters(branch, task_id, queue_order, status) VALUES (?, ?, ?, ?)",
        ("main", "task-head", 1, BranchWaiterStatus.LEASED.value),
    )
    sqlite_conn.execute(
        "INSERT INTO branch_waiters(branch, task_id, queue_order, status) VALUES (?, ?, ?, ?)",
        ("main", "task-next", 2, BranchWaiterStatus.QUEUED.value),
    )
    sqlite_conn.execute("INSERT INTO branch_tokens(branch, last_token) VALUES (?, ?)", ("main", 11))
    redis_lease.acquire(branch="main", task_id="task-head", fencing_token=99, ttl_sec=30)
    handler = UnlockHandler(
        _conn_factory(sqlite_conn),
        redis_lease,
        journal_writer,
        clock=FrozenClock(now),
    )

    handler.handle_unlock("main")

    row = sqlite_conn.execute(
        "SELECT status FROM branch_waiters WHERE branch = ? AND task_id = ?",
        ("main", "task-head"),
    ).fetchone()
    token_row = sqlite_conn.execute("SELECT last_token FROM branch_tokens WHERE branch = ?", ("main",)).fetchone()
    assert redis_client.get("lease:branch:main") is None
    assert row is not None
    assert row["status"] == BranchWaiterStatus.QUEUED.value
    assert token_row is not None
    assert token_row["last_token"] == 11
    journal_writer.append.assert_called_once()
    event = journal_writer.append.call_args.args[0]
    assert event.event_type == "internal.dispatch_attempt"
    assert event.payload["task_id"] == "task-head"
    assert event.payload["lease_branch"] == "main"


def _conn_factory(sqlite_conn: sqlite3.Connection) -> Callable[[], sqlite3.Connection]:
    row = sqlite_conn.execute("PRAGMA database_list").fetchone()
    assert row is not None
    db_path = Path(row["file"])

    def factory() -> sqlite3.Connection:
        conn = connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return factory
