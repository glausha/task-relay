from __future__ import annotations

from datetime import datetime, timedelta, timezone

from task_relay.breaker.circuit_breaker import CircuitBreaker
from task_relay.errors import FailureCode
from task_relay.rate.windows import should_stop_new_tasks


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


def test_should_stop_new_tasks_thresholds() -> None:
    assert should_stop_new_tasks(remaining=19, limit=100) is True
    assert should_stop_new_tasks(remaining=20, limit=100) is False
    assert should_stop_new_tasks(remaining=0, limit=0) is False
