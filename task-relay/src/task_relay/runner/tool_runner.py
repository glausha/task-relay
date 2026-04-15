from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable
from typing import Any

from task_relay.clock import Clock, SystemClock
from task_relay.config import Settings
from task_relay.ids import new_event_id
from task_relay.journal.writer import JournalWriter
from task_relay.runner.adapters.base import AdapterOutput
from task_relay.types import CanonicalEvent, JournalPosition, Plan, Source, TaskState


SIGTERM_GRACE_SECONDS = 15


class ToolRunner:
    def __init__(
        self,
        task_id: str,
        conn_factory: Callable[[], sqlite3.Connection],
        journal_writer: JournalWriter,
        redis_client: Any,
        settings: Settings,
        clock: Clock = SystemClock(),
    ) -> None:
        self._task_id = task_id
        self._conn_factory = conn_factory
        self._journal_writer = journal_writer
        self._redis_client = redis_client
        self._settings = settings
        self._clock = clock

    def run_planning(self, plan_input: dict[str, Any]) -> AdapterOutput:
        raise NotImplementedError("Phase 2: tool_runner subprocess")

    def run_executor(self, plan_json: dict[str, Any]) -> AdapterOutput:
        raise NotImplementedError("Phase 2: tool_runner subprocess")

    def run_review(self, plan: Plan, diff_ref: str) -> AdapterOutput:
        raise NotImplementedError("Phase 2: tool_runner subprocess")

    def observe_state_change(self, state: TaskState) -> None:
        raise NotImplementedError("Phase 2: tool_runner subprocess")

    def append_internal_event(self, event_type: str, payload: dict[str, Any]) -> JournalPosition:
        event_id = new_event_id()
        event = CanonicalEvent(
            event_id=event_id,
            source=Source.INTERNAL,
            delivery_id=event_id,
            event_type=event_type,
            payload={"task_id": self._task_id, **payload},
            received_at=self._clock.now(),
            request_id=None,
        )
        return self._journal_writer.append(event)


def _terminate_subprocess(
    proc: subprocess.Popen[Any],
    grace_seconds: int = SIGTERM_GRACE_SECONDS,
) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
