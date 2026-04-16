"""Admin unlock handler: detailed-design §5.5."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from task_relay.branch_lease.redis_lease import RedisLease
from task_relay.clock import Clock, SystemClock
from task_relay.ids import new_event_id
from task_relay.journal.writer import JournalWriter
from task_relay.router.transitions import requeue_branch_head_waiter
from task_relay.types import CanonicalEvent, Source


class UnlockHandler:
    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        redis_lease: RedisLease,
        journal_writer: JournalWriter,
        *,
        clock: Clock = SystemClock(),
    ) -> None:
        self._conn_factory = conn_factory
        self._redis_lease = redis_lease
        self._journal_writer = journal_writer
        self._clock = clock

    def handle_unlock(self, branch: str) -> None:
        if not branch:
            return
        self._redis_lease.force_release(branch)
        conn = self._conn_factory()
        try:
            conn.execute("BEGIN IMMEDIATE")
            task_id = requeue_branch_head_waiter(conn, branch)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        if task_id is None:
            return
        self._append_internal_event(
            "internal.dispatch_attempt",
            {
                "task_id": task_id,
                "lease_branch": branch,
                "trigger": "admin_unlock",
            },
        )

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
