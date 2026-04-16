from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_relay.clock import FrozenClock
from task_relay.db.connection import connect
from task_relay.journal.reader import JournalReader
from task_relay.journal.writer import JournalWriter
from task_relay.reconcile.worker import ReconcileWorker
from task_relay.types import Source, TaskState

from tests.unit._test_helpers import insert_plan_row, seed_task


def test_reconcile_emits_resume_event_for_implementing_task(sqlite_conn: sqlite3.Connection, tmp_path: Path) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    task = seed_task(
        sqlite_conn,
        task_id="task-impl",
        created_at=now - timedelta(hours=1),
        state=TaskState.IMPLEMENTING,
        last_known_head_commit="abc123",
    )
    insert_plan_row(
        sqlite_conn,
        task_id=task.task_id,
        plan_rev=4,
        plan_json={"allowed_files": ["src/a.py"], "acceptance_criteria": ["works"]},
        created_at=now - timedelta(minutes=10),
    )
    sqlite_conn.execute(
        """
        INSERT INTO tool_calls(
            call_id, task_id, stage, tool_name, started_at, ended_at, duration_ms,
            success, exit_code, failure_code, log_path, log_sha256, log_bytes,
            tokens_in, tokens_out
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "call-1",
            task.task_id,
            "executing",
            "executor",
            (now - timedelta(seconds=50)).isoformat(),
            (now - timedelta(seconds=30)).isoformat(),
            20_000,
            1,
            0,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
    )
    journal_dir = tmp_path / "journal"
    worker = ReconcileWorker(
        conn_factory=_conn_factory(sqlite_conn),
        journal_writer=JournalWriter(journal_dir, FrozenClock(now)),
        clock=FrozenClock(now),
    )

    result = worker.run_once()

    events = list(JournalReader(journal_dir).iterate_from(None))
    assert result == {"implementing_checked": 1, "events_emitted": 1, "degraded_aged": 0}
    assert len(events) == 1
    _, event = events[0]
    assert event.source is Source.INTERNAL
    assert event.event_type == "internal.reconcile_resume"
    assert event.payload == {
        "task_id": "task-impl",
        "plan_rev": 4,
        "lease_branch": None,
        "feature_branch": None,
        "worktree_path": None,
        "worktree_clean": True,
        "heartbeat_fresh": True,
        "last_known_head_commit": "abc123",
        "observed_at": "2026-04-15T12:00:00Z",
    }


def test_reconcile_checks_worktree_path_on_disk(
    sqlite_conn: sqlite3.Connection,
    git_repo: Path,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    worktree_path = tmp_path / "repo-worktree"
    subprocess.run(
        ["git", "-C", str(git_repo), "worktree", "add", "--detach", str(worktree_path), "main"],
        check=True,
        capture_output=True,
    )
    (worktree_path / "dirty.txt").write_text("dirty\n")
    seed_task(
        sqlite_conn,
        task_id="task-dirty",
        created_at=now - timedelta(hours=1),
        state=TaskState.IMPLEMENTING,
        worktree_path=str(worktree_path),
        last_known_head_commit="abc123",
    )
    journal_dir = tmp_path / "journal"
    worker = ReconcileWorker(
        conn_factory=_conn_factory(sqlite_conn),
        journal_writer=JournalWriter(journal_dir, FrozenClock(now)),
        clock=FrozenClock(now),
    )

    worker.run_once()

    events = list(JournalReader(journal_dir).iterate_from(None))
    assert len(events) == 1
    _, event = events[0]
    assert event.payload["task_id"] == "task-dirty"
    assert event.payload["worktree_clean"] is False


def test_reconcile_records_aged_system_degraded_task(sqlite_conn: sqlite3.Connection, tmp_path: Path) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    seed_task(
        sqlite_conn,
        task_id="task-degraded",
        created_at=now - timedelta(days=2),
        state=TaskState.SYSTEM_DEGRADED,
        updated_at=now - timedelta(hours=25),
    )
    worker = ReconcileWorker(
        conn_factory=_conn_factory(sqlite_conn),
        journal_writer=JournalWriter(tmp_path / "journal", FrozenClock(now)),
        clock=FrozenClock(now),
    )

    result = worker.run_once()

    row = sqlite_conn.execute(
        """
        SELECT task_id, event_type, severity, payload_json
        FROM system_events
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert result == {"implementing_checked": 0, "events_emitted": 0, "degraded_aged": 1}
    assert row is not None
    assert row["task_id"] == "task-degraded"
    assert row["event_type"] == "reconcile_degraded_aged"
    assert row["severity"] == "warning"
    assert json.loads(str(row["payload_json"])) == {"aged_hours": 25, "task_id": "task-degraded"}


def test_reconcile_ignores_non_target_states(sqlite_conn: sqlite3.Connection, tmp_path: Path) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    seed_task(
        sqlite_conn,
        task_id="task-planning",
        created_at=now - timedelta(hours=1),
        state=TaskState.PLANNING,
    )
    seed_task(
        sqlite_conn,
        task_id="task-done",
        created_at=now - timedelta(hours=2),
        state=TaskState.DONE,
    )
    journal_dir = tmp_path / "journal"
    worker = ReconcileWorker(
        conn_factory=_conn_factory(sqlite_conn),
        journal_writer=JournalWriter(journal_dir, FrozenClock(now)),
        clock=FrozenClock(now),
    )

    result = worker.run_once()

    files = sorted(journal_dir.glob("*.ndjson.zst"))
    row = sqlite_conn.execute("SELECT COUNT(*) AS count FROM system_events").fetchone()
    assert result == {"implementing_checked": 0, "events_emitted": 0, "degraded_aged": 0}
    assert files == []
    assert row is not None
    assert row["count"] == 0


def _conn_factory(sqlite_conn: sqlite3.Connection) -> Callable[[], sqlite3.Connection]:
    row = sqlite_conn.execute("PRAGMA database_list").fetchone()
    db_path = Path(row["file"])

    def factory() -> sqlite3.Connection:
        conn = connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return factory
