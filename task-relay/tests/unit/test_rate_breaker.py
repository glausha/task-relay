from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_relay.breaker.circuit_breaker import (
    EVENT_TYPE_BREAKER_FATAL_RECORDED,
    EVENT_TYPE_BREAKER_RESET,
    CircuitBreaker,
)
from task_relay.clock import FrozenClock
from task_relay.db.connection import connect
from task_relay.db.queries import insert_system_event
from task_relay.errors import FailureCode
from task_relay.rate.windows import should_stop_new_tasks
from task_relay.types import Severity


def test_circuit_breaker_opens_after_three_fatal_failures() -> None:
    base = datetime(2026, 4, 15, tzinfo=timezone.utc)
    breaker = CircuitBreaker()

    breaker.record(FailureCode.AUTH_ERROR, base)
    breaker.record(FailureCode.AUTH_ERROR, base + timedelta(seconds=1))
    breaker.record(FailureCode.AUTH_ERROR, base + timedelta(seconds=2))

    assert breaker.is_open(FailureCode.AUTH_ERROR, base + timedelta(seconds=3)) is True


def test_circuit_breaker_stays_closed_with_one_failure_and_resets() -> None:
    base = datetime(2026, 4, 15, tzinfo=timezone.utc)
    breaker = CircuitBreaker()

    breaker.record(FailureCode.PERMISSION_ERROR, base)

    assert breaker.is_open(FailureCode.PERMISSION_ERROR, base + timedelta(seconds=1)) is False
    breaker.record(FailureCode.PERMISSION_ERROR, base + timedelta(seconds=2))
    breaker.record(FailureCode.PERMISSION_ERROR, base + timedelta(seconds=3))
    assert breaker.is_open(FailureCode.PERMISSION_ERROR, base + timedelta(seconds=4)) is True

    breaker.reset(FailureCode.PERMISSION_ERROR)

    assert breaker.is_open(FailureCode.PERMISSION_ERROR, base + timedelta(seconds=5)) is False


def test_circuit_breaker_ignores_unknown_class_failures() -> None:
    breaker = CircuitBreaker()
    base = datetime(2026, 4, 15, tzinfo=timezone.utc)

    breaker.record(FailureCode.TIMEOUT, base)
    breaker.record(FailureCode.TIMEOUT, base + timedelta(seconds=1))
    breaker.record(FailureCode.TIMEOUT, base + timedelta(seconds=2))

    assert breaker.is_open(FailureCode.TIMEOUT, base + timedelta(seconds=3)) is False


def test_circuit_breaker_rebuild_from_events_opens_with_three_fatal_records(sqlite_conn: sqlite3.Connection) -> None:
    now = datetime(2026, 4, 15, 0, 10, tzinfo=timezone.utc)
    for offset in (3, 2, 1):
        _insert_breaker_event(
            sqlite_conn,
            event_type=EVENT_TYPE_BREAKER_FATAL_RECORDED,
            severity=Severity.WARNING,
            payload={
                "failure_code": FailureCode.AUTH_ERROR.value,
                "at": _to_iso(now - timedelta(seconds=offset)),
            },
            created_at=now - timedelta(seconds=offset),
        )

    breaker = CircuitBreaker(clock=FrozenClock(now))
    breaker.rebuild_from_events(sqlite_conn)

    assert breaker.is_open(FailureCode.AUTH_ERROR, now) is True


def test_circuit_breaker_rebuild_from_events_applies_reset(sqlite_conn: sqlite3.Connection) -> None:
    now = datetime(2026, 4, 15, 0, 10, tzinfo=timezone.utc)
    for offset in (4, 3, 2):
        _insert_breaker_event(
            sqlite_conn,
            event_type=EVENT_TYPE_BREAKER_FATAL_RECORDED,
            severity=Severity.WARNING,
            payload={
                "failure_code": FailureCode.PERMISSION_ERROR.value,
                "at": _to_iso(now - timedelta(seconds=offset)),
            },
            created_at=now - timedelta(seconds=offset),
        )
    _insert_breaker_event(
        sqlite_conn,
        event_type=EVENT_TYPE_BREAKER_RESET,
        severity=Severity.INFO,
        payload={"failure_code": FailureCode.PERMISSION_ERROR.value},
        created_at=now - timedelta(seconds=1),
    )

    breaker = CircuitBreaker(clock=FrozenClock(now))
    breaker.rebuild_from_events(sqlite_conn)

    assert breaker.is_open(FailureCode.PERMISSION_ERROR, now) is False


def test_circuit_breaker_rebuild_from_events_ignores_old_fatal_records(sqlite_conn: sqlite3.Connection) -> None:
    now = datetime(2026, 4, 15, 0, 10, tzinfo=timezone.utc)
    for minutes in (13, 12, 11):
        observed_at = now - timedelta(minutes=minutes)
        _insert_breaker_event(
            sqlite_conn,
            event_type=EVENT_TYPE_BREAKER_FATAL_RECORDED,
            severity=Severity.WARNING,
            payload={
                "failure_code": FailureCode.AUTH_ERROR.value,
                "at": _to_iso(observed_at),
            },
            created_at=observed_at,
        )

    breaker = CircuitBreaker(clock=FrozenClock(now))
    breaker.rebuild_from_events(sqlite_conn)

    assert breaker.is_open(FailureCode.AUTH_ERROR, now) is False


def test_circuit_breaker_record_persists_system_event(sqlite_conn: sqlite3.Connection) -> None:
    now = datetime(2026, 4, 15, 0, 10, tzinfo=timezone.utc)
    breaker = CircuitBreaker(clock=FrozenClock(now), conn_factory=_conn_factory(sqlite_conn))

    breaker.record(FailureCode.AUTH_ERROR, now)

    row = sqlite_conn.execute(
        """
        SELECT event_type, severity, payload_json
        FROM system_events
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()

    assert row is not None
    assert row["event_type"] == EVENT_TYPE_BREAKER_FATAL_RECORDED
    assert row["severity"] == Severity.WARNING.value
    assert json.loads(str(row["payload_json"])) == {
        "at": _to_iso(now),
        "failure_code": FailureCode.AUTH_ERROR.value,
    }


def test_should_stop_new_tasks_thresholds() -> None:
    assert should_stop_new_tasks(remaining=19, limit=100) is True
    assert should_stop_new_tasks(remaining=20, limit=100) is False
    assert should_stop_new_tasks(remaining=0, limit=0) is False


def _insert_breaker_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    severity: Severity,
    payload: dict[str, str],
    created_at: datetime,
) -> None:
    insert_system_event(
        conn,
        task_id=None,
        event_type=event_type,
        severity=severity,
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        created_at=created_at,
    )


def _conn_factory(sqlite_conn: sqlite3.Connection) -> Callable[[], sqlite3.Connection]:
    row = sqlite_conn.execute("PRAGMA database_list").fetchone()
    db_path = Path(row["file"])

    def factory() -> sqlite3.Connection:
        conn = connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return factory


def _to_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
