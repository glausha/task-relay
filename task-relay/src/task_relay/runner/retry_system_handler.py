"""Admin retry-system handler: detailed-design §8.3."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from task_relay.breaker.circuit_breaker import CircuitBreaker
from task_relay.clock import Clock, SystemClock
from task_relay.ids import new_event_id
from task_relay.journal.writer import JournalWriter
from task_relay.types import CanonicalEvent, Source, TaskState


class RetrySystemHandler:
    def __init__(
        self,
        breaker: CircuitBreaker,
        journal_writer: JournalWriter,
        *,
        conn_factory: Callable[[], sqlite3.Connection],
        redis_client: Any,
        clock: Clock = SystemClock(),
    ) -> None:
        self._breaker = breaker
        self._journal_writer = journal_writer
        self._conn_factory = conn_factory
        self._redis_client = redis_client
        self._clock = clock

    def handle_retry_system(self, stage: str | None = None) -> None:
        self._breaker.reset(failure_code=None)
        if not self._health_check():
            return
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                """
                SELECT task_id
                FROM tasks
                WHERE state = ?
                ORDER BY updated_at ASC, task_id ASC
                """,
                (TaskState.SYSTEM_DEGRADED.value,),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            payload: dict[str, object] = {"task_id": str(row["task_id"])}
            if stage is not None:
                payload["stage"] = stage
            self._append_internal_event("internal.system_recovered", payload)

    def _health_check(self) -> bool:
        conn = self._conn_factory()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return bool(self._redis_client.ping())

    def _append_internal_event(self, event_type: str, payload: dict[str, object]) -> None:
        received_at = self._clock.now()
        event_id = new_event_id()
        self._journal_writer.append(
            CanonicalEvent(
                event_id=event_id,
                source=Source.INTERNAL,
                delivery_id=event_id,
                event_type=event_type,
                payload=payload,
                received_at=received_at,
                request_id=None,
            )
        )
