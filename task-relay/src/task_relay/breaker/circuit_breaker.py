from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from task_relay.clock import Clock, SystemClock
from task_relay.db.queries import insert_system_event
from task_relay.errors import FAILURE_CLASS, FailureClass, FailureCode
from task_relay.types import Severity


WINDOW_SECONDS = 600
FATAL_THRESHOLD = 3
EVENT_TYPE_BREAKER_FATAL_RECORDED = "breaker_fatal_recorded"
EVENT_TYPE_BREAKER_RESET = "breaker_reset"


@dataclass
class _Bucket:
    failures: list[datetime] = field(default_factory=list)
    opened_at: datetime | None = None


class CircuitBreaker:
    def __init__(
        self,
        window_seconds: int = WINDOW_SECONDS,
        fatal_threshold: int = FATAL_THRESHOLD,
        clock: Clock = SystemClock(),
        conn_factory: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        self._window_seconds = window_seconds
        self._fatal_threshold = fatal_threshold
        self._clock = clock
        self._conn_factory = conn_factory
        self._buckets = {code: _Bucket() for code in FailureCode}

    def record(self, failure_code: FailureCode, at: datetime | None = None) -> None:
        if FAILURE_CLASS[failure_code] != FailureClass.FATAL:
            return
        observed_at = at or self._clock.now()
        self._record_fatal(failure_code, observed_at)
        self._append_system_event(
            event_type=EVENT_TYPE_BREAKER_FATAL_RECORDED,
            severity=Severity.WARNING,
            payload={
                "failure_code": failure_code.value,
                "at": self._to_iso(observed_at),
            },
            created_at=observed_at,
        )

    def is_open(self, failure_code: FailureCode, at: datetime | None = None) -> bool:
        if FAILURE_CLASS[failure_code] != FailureClass.FATAL:
            return False
        observed_at = at or self._clock.now()
        bucket = self._buckets[failure_code]
        self._prune(bucket, observed_at)
        if len(bucket.failures) >= self._fatal_threshold:
            if bucket.opened_at is None:
                bucket.opened_at = observed_at
            return True
        bucket.opened_at = None
        return False

    def open_codes(self, at: datetime | None = None) -> list[FailureCode]:
        observed_at = at or self._clock.now()
        return [code for code in FailureCode if self.is_open(code, observed_at)]

    def reset(self, failure_code: FailureCode | None = None) -> None:
        if failure_code is None:
            for bucket in self._buckets.values():
                bucket.failures.clear()
                bucket.opened_at = None
        else:
            self._clear_bucket(failure_code)
        self._append_system_event(
            event_type=EVENT_TYPE_BREAKER_RESET,
            severity=Severity.INFO,
            payload={"failure_code": "*" if failure_code is None else failure_code.value},
            created_at=self._clock.now(),
        )

    def rebuild_from_events(self, conn: sqlite3.Connection, window_seconds: int | None = None) -> None:
        now = self._clock.now()
        effective_window_seconds = self._window_seconds if window_seconds is None else window_seconds
        cutoff = self._to_iso(now - timedelta(seconds=effective_window_seconds))
        rows = conn.execute(
            """
            SELECT event_type, payload_json, created_at
            FROM system_events
            WHERE event_type IN (?, ?)
              AND created_at >= ?
            ORDER BY id ASC
            """,
            (EVENT_TYPE_BREAKER_FATAL_RECORDED, EVENT_TYPE_BREAKER_RESET, cutoff),
        ).fetchall()
        for bucket in self._buckets.values():
            bucket.failures.clear()
            bucket.opened_at = None
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if row["event_type"] == EVENT_TYPE_BREAKER_RESET:
                self._rebuild_reset(str(payload["failure_code"]))
                continue
            failure_code = FailureCode(str(payload["failure_code"]))
            observed_at = self._parse_iso(str(payload.get("at", row["created_at"])))
            self._record_fatal(failure_code, observed_at, window_seconds=effective_window_seconds)
        for code in FailureCode:
            if FAILURE_CLASS[code] != FailureClass.FATAL:
                continue
            bucket = self._buckets[code]
            self._prune(bucket, now, window_seconds=effective_window_seconds)
            if len(bucket.failures) < self._fatal_threshold:
                bucket.opened_at = None

    def _prune(self, bucket: _Bucket, now: datetime, *, window_seconds: int | None = None) -> None:
        effective_window_seconds = self._window_seconds if window_seconds is None else window_seconds
        cutoff = now - timedelta(seconds=effective_window_seconds)
        bucket.failures[:] = [failure_at for failure_at in bucket.failures if failure_at >= cutoff]

    def _record_fatal(
        self,
        failure_code: FailureCode,
        observed_at: datetime,
        *,
        window_seconds: int | None = None,
    ) -> None:
        bucket = self._buckets[failure_code]
        bucket.failures.append(observed_at)
        self._prune(bucket, observed_at, window_seconds=window_seconds)
        if len(bucket.failures) >= self._fatal_threshold:
            bucket.opened_at = bucket.opened_at or observed_at
            return
        bucket.opened_at = None

    def _clear_bucket(self, failure_code: FailureCode) -> None:
        bucket = self._buckets[failure_code]
        bucket.failures.clear()
        bucket.opened_at = None

    def _rebuild_reset(self, failure_code: str) -> None:
        if failure_code == "*":
            for code in FailureCode:
                self._clear_bucket(code)
            return
        self._clear_bucket(FailureCode(failure_code))

    def _append_system_event(
        self,
        *,
        event_type: str,
        severity: Severity,
        payload: dict[str, str],
        created_at: datetime,
    ) -> None:
        if self._conn_factory is None:
            return
        conn = self._conn_factory()
        try:
            insert_system_event(
                conn,
                task_id=None,
                event_type=event_type,
                severity=severity,
                payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                created_at=created_at,
            )
        finally:
            conn.close()

    @staticmethod
    def _parse_iso(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _to_iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
