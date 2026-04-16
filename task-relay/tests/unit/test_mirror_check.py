from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from task_relay.clock import FrozenClock
from task_relay.projection.mirror_check import check_mirror_consistency
from task_relay.types import Severity, SystemEventType


def test_check_mirror_consistency_returns_true_when_frontmatter_matches(
    sqlite_conn: sqlite3.Connection,
) -> None:
    body = "---\nstate: planning\nstate_rev: 3\n---\n\nBody"

    result = check_mirror_consistency(
        sqlite_conn,
        task_id="task-1",
        remote_body=body,
        expected_body=body,
    )

    row = sqlite_conn.execute("SELECT COUNT(*) AS count FROM system_events").fetchone()
    assert result is True
    assert row is not None
    assert row["count"] == 0


def test_check_mirror_consistency_records_warning_when_frontmatter_differs(
    sqlite_conn: sqlite3.Connection,
) -> None:
    fixed = datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)

    result = check_mirror_consistency(
        sqlite_conn,
        task_id="task-1",
        remote_body="---\nstate: done\nstate_rev: 3\n---\n\nBody",
        expected_body="---\nstate: planning\nstate_rev: 3\n---\n\nBody",
        clock=FrozenClock(fixed),
    )

    row = sqlite_conn.execute(
        """
        SELECT task_id, event_type, severity, payload_json, created_at
        FROM system_events
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()

    assert result is False
    assert row is not None
    assert row["task_id"] == "task-1"
    assert row["event_type"] == SystemEventType.MIRROR_READONLY_VIOLATION_DETECTED.value
    assert row["severity"] == Severity.WARNING.value
    assert row["created_at"] == "2026-04-16T00:00:00Z"
    assert json.loads(str(row["payload_json"])) == {
        "changed_fields": ["state"],
        "expected_frontmatter": {"state": "planning", "state_rev": 3},
        "remote_frontmatter": {"state": "done", "state_rev": 3},
    }


def test_check_mirror_consistency_handles_missing_frontmatter_gracefully(
    sqlite_conn: sqlite3.Connection,
) -> None:
    result = check_mirror_consistency(
        sqlite_conn,
        task_id="task-1",
        remote_body="No frontmatter here",
        expected_body="Still no frontmatter",
    )

    row = sqlite_conn.execute("SELECT COUNT(*) AS count FROM system_events").fetchone()
    assert result is True
    assert row is not None
    assert row["count"] == 0
