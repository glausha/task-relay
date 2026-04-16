from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import fakeredis

from task_relay.clock import FrozenClock
from task_relay.config import Settings
from task_relay.db.connection import connect
from task_relay.db.queries import get_latest_plan
from task_relay.errors import FailureCode
from task_relay.runner.adapters.planner import PlannerAdapter
from task_relay.runner.adapters.reviewer import ReviewerAdapter
from task_relay.runner.dispatcher import TaskDispatcher
from task_relay.types import BranchWaiterStatus, JournalPosition, TaskState

from tests.unit._fake_transports import FakeTransport
from tests.unit._test_helpers import seed_task


def test_step_appends_dispatch_attempt_when_lease_is_acquired(sqlite_conn: sqlite3.Connection) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    seed_task(
        sqlite_conn,
        task_id="task-dispatch",
        created_at=now,
        state=TaskState.PLAN_APPROVED,
        lease_branch="main",
    )
    sqlite_conn.execute(
        "INSERT INTO branch_waiters(branch, task_id, queue_order, status) VALUES (?, ?, ?, ?)",
        ("main", "task-dispatch", 1, BranchWaiterStatus.QUEUED.value),
    )
    journal_writer = Mock()
    journal_writer.append.return_value = JournalPosition(file="20260415.ndjson.zst", offset=0)
    dispatcher = _dispatcher(sqlite_conn, journal_writer=journal_writer, clock=FrozenClock(now))

    result = dispatcher.step()

    row = sqlite_conn.execute(
        "SELECT status FROM branch_waiters WHERE branch = ? AND task_id = ?",
        ("main", "task-dispatch"),
    ).fetchone()
    assert result == 1
    assert row is not None
    assert row["status"] == BranchWaiterStatus.LEASED.value
    journal_writer.append.assert_called_once()
    event = journal_writer.append.call_args.args[0]
    assert event.event_type == "internal.dispatch_attempt"
    assert event.payload == {
        "task_id": "task-dispatch",
        "lease_acquired": True,
        "lease_branch": "main",
        "fencing_token": 1,
    }


def test_step_returns_zero_when_breaker_is_open(sqlite_conn: sqlite3.Connection) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    journal_writer = Mock()
    journal_writer.append.return_value = JournalPosition(file="20260415.ndjson.zst", offset=0)
    dispatcher = _dispatcher(
        sqlite_conn,
        journal_writer=journal_writer,
        clock=FrozenClock(now),
        settings=Settings(breaker_fatal_threshold=1),
    )
    dispatcher._breaker.record(FailureCode.ADAPTER_PARSE_ERROR, now)

    result = dispatcher.step()

    assert result == 0
    journal_writer.append.assert_not_called()


def test_step_returns_zero_when_no_plan_approved_waiter_exists(sqlite_conn: sqlite3.Connection) -> None:
    dispatcher = _dispatcher(sqlite_conn, journal_writer=Mock())

    result = dispatcher.step()

    assert result == 0


def test_run_task_planning_calls_planner_and_appends_plan_ready(sqlite_conn: sqlite3.Connection) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    seed_task(sqlite_conn, task_id="task-plan", created_at=now, state=TaskState.PLANNING)
    journal_writer = Mock()
    journal_writer.append.return_value = JournalPosition(file="20260415.ndjson.zst", offset=0)
    transport = FakeTransport([{"payload": _valid_plan_json(), "tokens_in": 7, "tokens_out": 9}])
    dispatcher = _dispatcher(
        sqlite_conn,
        journal_writer=journal_writer,
        clock=FrozenClock(now),
        planner=PlannerAdapter(transport),
    )

    dispatcher.run_task("task-plan")

    plan = get_latest_plan(sqlite_conn, "task-plan")
    assert transport.call_count == 1
    assert plan is not None
    assert plan.plan_rev == 1
    assert plan.planner_version == "planner-v2"
    journal_writer.append.assert_called_once()
    event = journal_writer.append.call_args.args[0]
    assert event.event_type == "internal.plan_ready"
    assert event.payload["task_id"] == "task-plan"
    assert event.payload["plan_rev"] == 1


def _dispatcher(
    sqlite_conn: sqlite3.Connection,
    *,
    journal_writer: Mock,
    clock=FrozenClock(datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)),
    settings: Settings | None = None,
    planner: PlannerAdapter | None = None,
) -> TaskDispatcher:
    effective_settings = settings or Settings()
    redis_client = fakeredis.FakeStrictRedis(decode_responses=True)
    effective_planner = planner or PlannerAdapter(FakeTransport([{"payload": _valid_plan_json()}]))
    reviewer = ReviewerAdapter(FakeTransport([{"payload": {"decision": "pass", "criteria": []}}]))
    return TaskDispatcher(
        conn_factory=_conn_factory(sqlite_conn),
        journal_writer=journal_writer,
        settings=effective_settings,
        redis_client=redis_client,
        planner=effective_planner,
        reviewer=reviewer,
        clock=clock,
    )


def _conn_factory(sqlite_conn: sqlite3.Connection) -> Callable[[], sqlite3.Connection]:
    row = sqlite_conn.execute("PRAGMA database_list").fetchone()
    assert row is not None
    db_path = Path(row["file"])

    def factory() -> sqlite3.Connection:
        conn = connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return factory


def _valid_plan_json() -> dict[str, object]:
    return {
        "goal": "Implement dispatcher",
        "sub_tasks": ["dispatch plan approved tasks"],
        "allowed_files": ["src/task_relay/**"],
        "auto_allowed_patterns": ["tests/**"],
        "acceptance_criteria": ["tests pass"],
        "forbidden_changes": ["no schema changes"],
        "risk_notes": ["lease churn"],
    }
