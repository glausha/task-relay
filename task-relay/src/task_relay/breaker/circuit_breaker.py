from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from task_relay.clock import Clock, SystemClock
from task_relay.errors import FAILURE_CLASS, FailureClass, FailureCode


WINDOW_SECONDS = 600
FATAL_THRESHOLD = 3


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
    ) -> None:
        self._window_seconds = window_seconds
        self._fatal_threshold = fatal_threshold
        self._clock = clock
        self._buckets = {code: _Bucket() for code in FailureCode}

    def record(self, failure_code: FailureCode, at: datetime | None = None) -> None:
        if FAILURE_CLASS[failure_code] != FailureClass.FATAL:
            return
        observed_at = at or self._clock.now()
        bucket = self._buckets[failure_code]
        bucket.failures.append(observed_at)
        self._prune(bucket, observed_at)
        if len(bucket.failures) >= self._fatal_threshold:
            bucket.opened_at = bucket.opened_at or observed_at
        else:
            bucket.opened_at = None

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
            return
        bucket = self._buckets[failure_code]
        bucket.failures.clear()
        bucket.opened_at = None

    def _prune(self, bucket: _Bucket, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._window_seconds)
        bucket.failures[:] = [failure_at for failure_at in bucket.failures if failure_at >= cutoff]
