from __future__ import annotations

import sqlite3

from task_relay.clock import Clock, SystemClock
from task_relay.projection.labels import MANAGED_LABELS


def rebuild_for_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    force: bool = False,
    clock: Clock = SystemClock(),
) -> int:
    _ = (conn, task_id, force, clock, MANAGED_LABELS)
    raise NotImplementedError("Phase 2: rebuild")
