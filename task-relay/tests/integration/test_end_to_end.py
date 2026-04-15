from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_relay.clock import FrozenClock
from task_relay.config import Settings
from task_relay.db import queries
from task_relay.db.connection import connect
from task_relay.db.migrations import apply_schema
from task_relay.ingester.journal_ingester import JournalIngester
from task_relay.ingress.cli_source import build_cli_event, build_ingress_issue_event
from task_relay.journal.reader import JournalReader
from task_relay.journal.writer import JournalWriter
from task_relay.projection import LoggingSink
from task_relay.projection.worker import ProjectionWorker
from task_relay.router.router import Router
from task_relay.types import Source, Stream, TaskState


def test_end_to_end_issue_opened_reaches_projection_sink(tmp_path: Path) -> None:
    event_time = datetime(2026, 4, 15, 9, 0, tzinfo=timezone.utc)
    sqlite_path = tmp_path / "state.sqlite"
    journal_dir = tmp_path / "journal"
    conn = connect(sqlite_path)
    apply_schema(conn)
    settings = Settings(forgejo_base_url="http://forgejo.local")
    writer = JournalWriter(journal_dir, FrozenClock(event_time))
    reader = JournalReader(journal_dir)
    ingester = JournalIngester(
        conn_factory=_conn_factory(sqlite_path),
        journal_reader=reader,
        clock=FrozenClock(event_time + timedelta(seconds=1)),
    )
    router = Router(settings, clock=FrozenClock(event_time))
    sink = LoggingSink()
    projection_worker = ProjectionWorker(
        conn,
        sinks={Stream.TASK_SNAPSHOT: sink},
        settings=settings,
        worker_id="projection-1",
        clock=FrozenClock(event_time + timedelta(seconds=settings.projection_retry_initial_seconds)),
    )
    event = build_ingress_issue_event(
        source=Source.FORGEJO,
        event_type="issues.opened",
        delivery_id="delivery-issue-opened",
        payload={"source_issue_id": "42", "requested_by": "alice"},
        clock=FrozenClock(event_time),
    )

    writer.append(event)
    assert ingester.step() == 1

    inbox_event = queries.fetch_next_unprocessed(conn)
    assert inbox_event is not None
    result = router.run_once(conn, inbox_event)
    task = queries.get_task(conn, result.task_id)
    outbox_row = conn.execute(
        """
        SELECT stream, state_rev
        FROM projection_outbox
        WHERE origin_event_id = ?
        ORDER BY outbox_id ASC
        """,
        (event.event_id,),
    ).fetchone()

    assert result.skipped is False
    assert result.to_state is TaskState.PLANNING
    assert task is not None
    assert task.state is TaskState.PLANNING
    assert outbox_row is not None
    assert outbox_row["stream"] == Stream.TASK_SNAPSHOT.value
    assert projection_worker.step() == 1
    assert len(sink.records) == 1
    assert sink.records[0].payload["state"] == TaskState.PLANNING.value

    cursor_row = conn.execute(
        """
        SELECT last_sent_state_rev
        FROM projection_cursors
        WHERE task_id = ? AND stream = ? AND target = ?
        """,
        (task.task_id, Stream.TASK_SNAPSHOT.value, "42"),
    ).fetchone()
    assert cursor_row is not None
    assert cursor_row["last_sent_state_rev"] == outbox_row["state_rev"]
    assert projection_worker.step() == 0
    writer.close()
    conn.close()


def test_end_to_end_cancel_event_updates_task_state(tmp_path: Path) -> None:
    event_time = datetime(2026, 4, 15, 9, 0, tzinfo=timezone.utc)
    sqlite_path = tmp_path / "state.sqlite"
    journal_dir = tmp_path / "journal"
    conn = connect(sqlite_path)
    apply_schema(conn)
    writer = JournalWriter(journal_dir, FrozenClock(event_time))
    ingester = JournalIngester(
        conn_factory=_conn_factory(sqlite_path),
        journal_reader=JournalReader(journal_dir),
        clock=FrozenClock(event_time + timedelta(seconds=1)),
    )
    router = Router(Settings(), clock=FrozenClock(event_time))
    issue_event = build_ingress_issue_event(
        source=Source.CLI,
        event_type="issues.opened",
        delivery_id="delivery-seed",
        payload={"source_issue_id": "77", "requested_by": "alice"},
        clock=FrozenClock(event_time),
    )

    writer.append(issue_event)
    assert ingester.step() == 1
    opened_inbox_event = queries.fetch_next_unprocessed(conn)
    assert opened_inbox_event is not None
    opened_result = router.run_once(conn, opened_inbox_event)

    cancel_event = build_cli_event(
        event_type="/cancel",
        task_id=opened_result.task_id,
        actor="alice",
        clock=FrozenClock(event_time + timedelta(minutes=1)),
    )
    writer.append(cancel_event)
    assert ingester.step() == 1

    cancel_inbox_event = queries.fetch_next_unprocessed(conn)
    assert cancel_inbox_event is not None
    cancel_result = router.run_once(conn, cancel_inbox_event)
    task = queries.get_task(conn, opened_result.task_id)

    assert cancel_result.skipped is False
    assert cancel_result.to_state is TaskState.CANCELLED
    assert task is not None
    assert task.state is TaskState.CANCELLED
    writer.close()
    conn.close()


def _conn_factory(sqlite_path: Path) -> Callable[[], sqlite3.Connection]:
    def factory() -> sqlite3.Connection:
        conn = connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    return factory
