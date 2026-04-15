from __future__ import annotations

import json
from datetime import datetime, timezone

from task_relay.db.queries import (
    fetch_next_unprocessed,
    get_task,
    insert_event,
    insert_outbox,
    insert_system_event,
    mark_outbox_sent,
    mark_processed,
    update_task_state,
    update_task_worktree,
    update_task_notification_target,
    upsert_task_on_create,
    claim_next_outbox,
)
from task_relay.types import InboxEvent, Severity, Source, Stream, TaskState


def test_task_round_trip(sqlite_conn) -> None:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)

    upsert_task_on_create(
        sqlite_conn,
        task_id="task-1",
        source_issue_id="issue-1",
        requested_by="forgejo:alice",
        lease_branch="main",
        feature_branch="task-relay/task-1",
        worktree_path="/tmp/task-1",
        notification_target=None,
        created_at=now,
        updated_at=now,
    )

    task = get_task(sqlite_conn, "task-1")

    assert task is not None
    assert task.task_id == "task-1"
    assert task.source_issue_id == "issue-1"
    assert task.state is TaskState.NEW
    assert task.lease_branch == "main"
    assert task.feature_branch == "task-relay/task-1"
    assert task.worktree_path == "/tmp/task-1"
    assert task.requested_by == "forgejo:alice"
    assert task.notification_target is None


def test_update_task_notification_target(sqlite_conn) -> None:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    upsert_task_on_create(
        sqlite_conn,
        task_id="task-1",
        source_issue_id="issue-1",
        requested_by="forgejo:alice",
        notification_target=None,
        created_at=now,
        updated_at=now,
    )

    update_task_notification_target(sqlite_conn, "task-1", "42")

    task = get_task(sqlite_conn, "task-1")
    assert task is not None
    assert task.notification_target == "42"


def test_update_task_state_can_set_and_clear_branch_metadata(sqlite_conn) -> None:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    upsert_task_on_create(
        sqlite_conn,
        task_id="task-branches",
        source_issue_id="issue-1",
        requested_by="forgejo:alice",
        created_at=now,
        updated_at=now,
    )

    update_task_state(
        sqlite_conn,
        task_id="task-branches",
        new_state=TaskState.IMPLEMENTING,
        new_state_rev=1,
        updated_at=now,
        lease_branch="main",
        feature_branch="task-relay/task-branches",
        worktree_path="/tmp/task-branches",
    )
    update_task_state(
        sqlite_conn,
        task_id="task-branches",
        new_state=TaskState.PLAN_APPROVED,
        new_state_rev=2,
        updated_at=now,
        resume_target_state=None,
        feature_branch=None,
        worktree_path=None,
    )

    task = get_task(sqlite_conn, "task-branches")
    assert task is not None
    assert task.state is TaskState.PLAN_APPROVED
    assert task.lease_branch == "main"
    assert task.feature_branch is None
    assert task.worktree_path is None


def test_update_task_worktree_updates_all_worktree_columns(sqlite_conn) -> None:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    upsert_task_on_create(
        sqlite_conn,
        task_id="task-worktree",
        source_issue_id="issue-1",
        requested_by="forgejo:alice",
        created_at=now,
        updated_at=now,
    )

    update_task_worktree(
        sqlite_conn,
        "task-worktree",
        lease_branch="release/1.0",
        feature_branch="task-relay/task-worktree",
        worktree_path="/workspace/task-worktree",
    )

    task = get_task(sqlite_conn, "task-worktree")
    assert task is not None
    assert task.lease_branch == "release/1.0"
    assert task.feature_branch == "task-relay/task-worktree"
    assert task.worktree_path == "/workspace/task-worktree"


def test_event_inbox_round_trip(sqlite_conn) -> None:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    event = InboxEvent(
        event_id="evt-1",
        source=Source.DISCORD,
        delivery_id="delivery-1",
        event_type="task.created",
        payload={"task": "task-1"},
        journal_offset=12,
        received_at=now,
    )

    inserted = insert_event(sqlite_conn, event)
    pending = fetch_next_unprocessed(sqlite_conn)
    mark_processed(sqlite_conn, event.event_id, now)
    after = fetch_next_unprocessed(sqlite_conn)

    assert inserted is True
    assert pending == event
    assert after is None


def test_outbox_round_trip(sqlite_conn) -> None:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    upsert_task_on_create(
        sqlite_conn,
        task_id="task-1",
        source_issue_id=None,
        requested_by="forgejo:alice",
        notification_target=None,
        created_at=now,
        updated_at=now,
    )

    outbox_id = insert_outbox(
        sqlite_conn,
        task_id="task-1",
        stream=Stream.TASK_COMMENT,
        target="issue-1",
        origin_event_id="evt-1",
        payload_json=json.dumps({"body": "hello"}),
        state_rev=0,
        idempotency_key="idem-1",
        next_attempt_at="2026-04-15T00:00:00Z",
    )
    claimed = claim_next_outbox(sqlite_conn, worker_id="worker-1", now_iso="2026-04-15T00:00:00Z")
    mark_outbox_sent(sqlite_conn, outbox_id, now)

    row = sqlite_conn.execute(
        "SELECT sent_at FROM projection_outbox WHERE outbox_id = ?",
        (outbox_id,),
    ).fetchone()

    assert claimed is not None
    assert claimed.outbox_id == outbox_id
    assert claimed.stream is Stream.TASK_COMMENT
    assert row is not None
    assert row["sent_at"] == "2026-04-15T00:00:00Z"


def test_insert_system_event(sqlite_conn) -> None:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)

    event_id = insert_system_event(
        sqlite_conn,
        task_id="task-1",
        event_type="retention_orphan_detected",
        severity=Severity.WARNING,
        payload_json='{"kind":"orphan"}',
        created_at=now,
    )

    row = sqlite_conn.execute(
        "SELECT id, severity FROM system_events WHERE id = ?",
        (event_id,),
    ).fetchone()

    assert row is not None
    assert row["severity"] == Severity.WARNING.value
