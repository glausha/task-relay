from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_relay.clock import FrozenClock
from task_relay.retention.log_retention import LogRetention
from task_relay.types import TaskState


def test_log_retention_nulls_old_log_path_and_deletes_file(
    sqlite_conn,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    log_dir = tmp_path / "logs"
    log_path = log_dir / "task-1" / "planning" / "old.jsonl.zst"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"payload")
    _insert_task(sqlite_conn, task_id="task-1", now=now)
    started_at = now - timedelta(days=31)
    sqlite_conn.execute(
        """
        INSERT INTO tool_calls(
            call_id, task_id, stage, tool_name, started_at, ended_at, duration_ms,
            success, exit_code, failure_code, log_path, log_sha256, log_bytes, tokens_in, tokens_out
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "call-1",
            "task-1",
            "planning",
            "planner",
            started_at.isoformat(),
            None,
            None,
            None,
            None,
            None,
            str(log_path),
            "deadbeef",
            7,
            None,
            None,
        ),
    )
    retention = LogRetention(lambda: sqlite_conn, log_dir, FrozenClock(now))

    result = retention.sweep()

    row = sqlite_conn.execute(
        "SELECT log_path, log_sha256, log_bytes FROM tool_calls WHERE call_id = ?",
        ("call-1",),
    ).fetchone()
    assert result["nulled"] == 1
    assert result["deleted_files"] == 1
    assert tuple(row) == (None, None, None)
    assert log_path.exists() is False


def test_log_retention_deletes_orphan_file_and_appends_system_event(
    sqlite_conn,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    log_dir = tmp_path / "logs"
    orphan_path = log_dir / "task-9" / "reviewing" / "orphan.jsonl.zst"
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_bytes(b"orphan")
    retention = LogRetention(lambda: sqlite_conn, log_dir, FrozenClock(now))

    result = retention.sweep()

    event_row = sqlite_conn.execute(
        "SELECT event_type, severity, payload_json FROM system_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert result["orphan_files"] == 1
    assert orphan_path.exists() is False
    assert event_row is not None
    assert event_row[0] == "retention_orphan_detected"
    assert event_row[1] == "warning"
    payload = json.loads(event_row[2])
    assert payload["orphan_kind"] == "file"
    assert payload["detected_by"] == "retention_sweep"


def _insert_task(sqlite_conn, *, task_id: str, now: datetime) -> None:
    sqlite_conn.execute(
        """
        INSERT INTO tasks(
            task_id, source_issue_id, state, state_rev, critical, lease_branch,
            feature_branch, manual_gate_required, worktree_path, last_known_head_commit, resume_target_state,
            requested_by, notification_target, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            None,
            TaskState.NEW.value,
            0,
            0,
            None,
            None,
            0,
            None,
            None,
            None,
            "tester",
            None,
            now.isoformat(),
            now.isoformat(),
        ),
    )
