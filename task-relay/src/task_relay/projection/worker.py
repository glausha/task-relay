from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Protocol

from task_relay.config import Settings
from task_relay.db.queries import (
    claim_next_outbox,
    get_cursor,
    mark_outbox_sent,
    reclaim_stale_outbox,
    reschedule_outbox,
    upsert_cursor,
)
from task_relay.clock import Clock, SystemClock
from task_relay.system_events import append_system_event
from task_relay.types import OutboxRecord, Severity, Stream


class ProjectionSink(Protocol):
    def send(self, record: OutboxRecord) -> None: ...


class ProjectionWorker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        sinks: dict[Stream, ProjectionSink],
        settings: Settings,
        worker_id: str,
        clock: Clock = SystemClock(),
        stale_claim_seconds: int | None = None,
    ) -> None:
        self._conn = conn
        self._sinks = sinks
        self._settings = settings
        self._worker_id = worker_id
        self._clock = clock
        self._stale_claim_seconds = (
            settings.projection_stale_claim_seconds if stale_claim_seconds is None else stale_claim_seconds
        )

    def step(self) -> int:
        now = self._clock.now()
        # WHY: a crashed worker must not permanently block older rows in the same projection lane.
        reclaim_stale_outbox(
            self._conn,
            now_iso=now.isoformat(),
            stale_after_seconds=self._stale_claim_seconds,
        )
        record = claim_next_outbox(self._conn, self._worker_id, now.isoformat())
        if record is None:
            return 0
        if record.stream is Stream.TASK_SNAPSHOT and self._is_superseded(record):
            mark_outbox_sent(self._conn, record.outbox_id, now)
            return 1
        sink = self._sinks.get(record.stream)
        if sink is None:
            raise KeyError(f"missing sink for stream={record.stream.value}")
        try:
            sink.send(record)
        except Exception as exc:
            self._handle_failure(record=record, error=exc, now=now)
            return 1
        mark_outbox_sent(self._conn, record.outbox_id, now)
        if record.stream is Stream.TASK_SNAPSHOT:
            upsert_cursor(
                self._conn,
                task_id=record.task_id,
                stream=record.stream,
                target=record.target,
                last_sent_state_rev=record.state_rev,
                last_sent_outbox_id=record.outbox_id,
                updated_at=now,
            )
        return 1

    def run_forever(self, poll_interval_sec: float = 0.5) -> None:
        while True:
            if self.step() == 0:
                time.sleep(poll_interval_sec)

    def _is_superseded(self, record: OutboxRecord) -> bool:
        cursor = get_cursor(self._conn, record.task_id, record.stream, record.target)
        return cursor is not None and cursor[0] >= record.state_rev

    def _handle_failure(self, *, record: OutboxRecord, error: Exception, now: datetime) -> None:
        attempt_count = record.attempt_count + 1
        next_attempt_at = now + timedelta(seconds=self._backoff_seconds(attempt_count))
        reschedule_outbox(self._conn, record.outbox_id, next_attempt_at, attempt_count)
        if not self._should_degrade(record=record, attempt_count=attempt_count, now=now):
            return
        payload = json.dumps(
            {
                "attempt_count": attempt_count,
                "error": str(error),
                "outbox_id": record.outbox_id,
                "stream": record.stream.value,
                "target": record.target,
                "worker_id": self._worker_id,
            },
            sort_keys=True,
        )
        append_system_event(
            self._conn,
            task_id=record.task_id,
            event_type="internal.infra_fatal",
            severity=Severity.ERROR,
            payload_json=payload,
            created_at_iso=now.isoformat(),
        )

    def _backoff_seconds(self, attempt_count: int) -> int:
        initial = self._settings.projection_retry_initial_seconds
        cap = self._settings.projection_retry_cap_seconds
        return min(initial * (2 ** max(attempt_count - 1, 0)), cap)

    def _should_degrade(self, *, record: OutboxRecord, attempt_count: int, now: datetime) -> bool:
        if attempt_count >= self._settings.projection_retry_max_attempts:
            return True
        return now - record.next_attempt_at >= timedelta(hours=self._settings.projection_retry_degrade_hours)
