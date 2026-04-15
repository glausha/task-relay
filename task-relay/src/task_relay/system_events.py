from __future__ import annotations

import sqlite3
from datetime import datetime

from task_relay.db.queries import insert_system_event
from task_relay.types import Severity


def append_system_event(
    conn: sqlite3.Connection,
    *,
    task_id: str | None,
    event_type: str,
    severity: Severity,
    payload_json: str,
    created_at_iso: str,
) -> int:
    return insert_system_event(
        conn,
        task_id=task_id,
        event_type=event_type,
        severity=severity,
        payload_json=payload_json,
        created_at=datetime.fromisoformat(created_at_iso.replace("Z", "+00:00")),
    )
