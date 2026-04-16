from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from task_relay.clock import FrozenClock
from task_relay.config import Settings
from task_relay.db.queries import insert_outbox, upsert_cursor, upsert_task_on_create
from task_relay.projection import LoggingSink
from task_relay.projection.worker import ProjectionWorker
from task_relay.types import Stream


class FailingSink:
    def send(self, record) -> None:
        _ = record
        raise RuntimeError("boom")


def test_projection_worker_marks_sent_on_success(sqlite_conn: sqlite3.Connection) -> None:
    fixed = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    outbox_id = _insert_task_and_outbox(
        sqlite_conn,
        now=fixed,
        stream=Stream.TASK_COMMENT,
        target="org/repo",
        payload={"body": "hello", "issue_number": 7},
        state_rev=1,
    )
    sink = LoggingSink()
    worker = ProjectionWorker(
        sqlite_conn,
        sinks={Stream.TASK_COMMENT: sink},
        settings=Settings(),
        worker_id="worker-1",
        clock=FrozenClock(fixed),
    )
    assert worker.step() == 1
    row = sqlite_conn.execute(
        "SELECT sent_at FROM projection_outbox WHERE outbox_id = ?",
        (outbox_id,),
    ).fetchone()
    assert row is not None
    assert row["sent_at"] == _iso_z(fixed)
    assert len(sink.records) == 1


def test_projection_worker_skips_superseded_snapshot(sqlite_conn: sqlite3.Connection) -> None:
    fixed = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    outbox_id = _insert_task_and_outbox(
        sqlite_conn,
        now=fixed,
        stream=Stream.TASK_SNAPSHOT,
        target="org/repo",
        payload={"body": "snapshot", "issue_number": 7},
        state_rev=3,
    )
    upsert_cursor(
        sqlite_conn,
        task_id="task-1",
        stream=Stream.TASK_SNAPSHOT,
        target="org/repo",
        last_sent_state_rev=4,
        last_sent_outbox_id=999,
        updated_at=fixed,
    )
    sink = LoggingSink()
    worker = ProjectionWorker(
        sqlite_conn,
        sinks={Stream.TASK_SNAPSHOT: sink},
        settings=Settings(),
        worker_id="worker-1",
        clock=FrozenClock(fixed),
    )
    assert worker.step() == 1
    row = sqlite_conn.execute(
        "SELECT sent_at FROM projection_outbox WHERE outbox_id = ?",
        (outbox_id,),
    ).fetchone()
    assert row is not None
    assert row["sent_at"] == _iso_z(fixed)
    assert sink.records == []


def test_projection_worker_reschedules_after_sink_failure(sqlite_conn: sqlite3.Connection) -> None:
    fixed = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    original_next_attempt_at = fixed - timedelta(minutes=5)
    outbox_id = _insert_task_and_outbox(
        sqlite_conn,
        now=original_next_attempt_at,
        stream=Stream.TASK_COMMENT,
        target="org/repo",
        payload={"body": "hello", "issue_number": 7},
        state_rev=1,
    )
    worker = ProjectionWorker(
        sqlite_conn,
        sinks={Stream.TASK_COMMENT: FailingSink()},
        settings=Settings(),
        worker_id="worker-1",
        clock=FrozenClock(fixed),
    )
    assert worker.step() == 1
    row = sqlite_conn.execute(
        """
        SELECT attempt_count, next_attempt_at, sent_at, claimed_by
        FROM projection_outbox
        WHERE outbox_id = ?
        """,
        (outbox_id,),
    ).fetchone()
    assert row is not None
    assert row["attempt_count"] == 1
    assert row["sent_at"] is None
    assert row["claimed_by"] is None
    assert datetime.fromisoformat(str(row["next_attempt_at"])) > original_next_attempt_at


def test_projection_worker_reclaims_stale_claim_before_processing_next_row(sqlite_conn: sqlite3.Connection) -> None:
    fixed = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    first_outbox_id = _insert_task_and_outbox(
        sqlite_conn,
        now=fixed - timedelta(hours=2),
        stream=Stream.TASK_COMMENT,
        target="org/repo",
        payload={"body": "first", "issue_number": 7},
        state_rev=1,
    )
    second_outbox_id = _insert_task_and_outbox(
        sqlite_conn,
        now=fixed - timedelta(hours=1),
        stream=Stream.TASK_COMMENT,
        target="org/repo",
        payload={"body": "second", "issue_number": 7},
        state_rev=2,
    )
    sqlite_conn.execute(
        """
        UPDATE projection_outbox
        SET claimed_by = ?, claimed_at = ?
        WHERE outbox_id = ?
        """,
        ("dead-worker", _iso_z(fixed - timedelta(hours=2)), first_outbox_id),
    )
    sink = LoggingSink()
    worker = ProjectionWorker(
        sqlite_conn,
        sinks={Stream.TASK_COMMENT: sink},
        settings=Settings(projection_stale_claim_seconds=600),
        worker_id="worker-1",
        clock=FrozenClock(fixed),
    )

    assert worker.step() == 1
    assert worker.step() == 1
    assert worker.step() == 0

    rows = sqlite_conn.execute(
        """
        SELECT outbox_id, sent_at, claimed_by
        FROM projection_outbox
        WHERE outbox_id IN (?, ?)
        ORDER BY outbox_id
        """,
        (first_outbox_id, second_outbox_id),
    ).fetchall()

    assert len(sink.records) == 2
    assert [record.outbox_id for record in sink.records] == [first_outbox_id, second_outbox_id]
    assert rows[0]["sent_at"] == _iso_z(fixed)
    assert rows[0]["claimed_by"] == "worker-1"
    assert rows[1]["sent_at"] == _iso_z(fixed)
    assert rows[1]["claimed_by"] == "worker-1"


def _insert_task_and_outbox(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    stream: Stream,
    target: str,
    payload: dict[str, object],
    state_rev: int,
) -> int:
    upsert_task_on_create(
        conn,
        task_id="task-1",
        source_issue_id="issue-1",
        requested_by="discord:42",
        notification_target="42",
        created_at=now,
        updated_at=now,
    )
    return insert_outbox(
        conn,
        task_id="task-1",
        stream=stream,
        target=target,
        origin_event_id="event-1",
        payload_json=json.dumps(payload),
        state_rev=state_rev,
        idempotency_key=f"{stream.value}-key-{state_rev}",
        next_attempt_at=now.isoformat(),
    )


def _iso_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
