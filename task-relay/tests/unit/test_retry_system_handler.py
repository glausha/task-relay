from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import fakeredis

from task_relay.clock import FrozenClock
from task_relay.db.connection import connect
from task_relay.runner.retry_system_handler import RetrySystemHandler
from task_relay.types import JournalPosition, TaskState

from tests.unit._test_helpers import seed_task


def test_handle_retry_system_resets_breaker_and_appends_recovery_event(sqlite_conn: sqlite3.Connection) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    seed_task(
        sqlite_conn,
        task_id="task-degraded",
        created_at=now,
        state=TaskState.SYSTEM_DEGRADED,
        resume_target_state=TaskState.IMPLEMENTING,
    )
    breaker = Mock()
    journal_writer = Mock()
    journal_writer.append.return_value = JournalPosition(file="20260415.ndjson.zst", offset=0)
    handler = RetrySystemHandler(
        breaker,
        journal_writer,
        conn_factory=_conn_factory(sqlite_conn),
        redis_client=fakeredis.FakeStrictRedis(decode_responses=True),
        clock=FrozenClock(now),
    )

    handler.handle_retry_system("executor")

    breaker.reset.assert_called_once_with(failure_code=None)
    journal_writer.append.assert_called_once()
    event = journal_writer.append.call_args.args[0]
    assert event.event_type == "internal.system_recovered"
    assert event.payload == {"task_id": "task-degraded", "stage": "executor"}


def _conn_factory(sqlite_conn: sqlite3.Connection) -> Callable[[], sqlite3.Connection]:
    row = sqlite_conn.execute("PRAGMA database_list").fetchone()
    assert row is not None
    db_path = Path(row["file"])

    def factory() -> sqlite3.Connection:
        conn = connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return factory
