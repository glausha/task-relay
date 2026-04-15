from __future__ import annotations

from datetime import datetime, timezone

from task_relay.config import Settings
from task_relay.db import queries
from task_relay.ids import new_task_id_from_event
from task_relay.router.router import Router
from task_relay.types import InboxEvent, Source, Task, TaskState

from tests.unit._test_helpers import insert_plan_row, seed_task


def _event(
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    source: Source = Source.INTERNAL,
) -> InboxEvent:
    return InboxEvent(
        event_id=event_id,
        source=source,
        delivery_id=f"delivery-{event_id}",
        event_type=event_type,
        payload=payload,
        journal_offset=0,
        received_at=datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc),
    )


def _seed_task(sqlite_conn, task_id: str, *, state: TaskState, critical: bool = False) -> Task:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    return seed_task(
        sqlite_conn,
        task_id=task_id,
        created_at=now,
        state=state,
        critical=critical,
    )


def test_new_issue_opened_transitions_to_planning_with_deterministic_task_id(sqlite_conn) -> None:
    router = Router(Settings())
    event = _event(
        event_id="evt-opened",
        event_type="issues.opened",
        payload={"source_issue_id": "42", "sender_login": "alice"},
        source=Source.FORGEJO,
    )
    queries.insert_event(sqlite_conn, event)

    result = router.run_once(sqlite_conn, event)

    task = queries.get_task(sqlite_conn, result.task_id)
    task_row = sqlite_conn.execute(
        "SELECT state, state_rev FROM tasks WHERE task_id = ?",
        (result.task_id,),
    ).fetchone()
    stream_rows = sqlite_conn.execute(
        "SELECT stream FROM projection_outbox WHERE origin_event_id = ? ORDER BY outbox_id",
        (event.event_id,),
    ).fetchall()

    assert result.task_id == new_task_id_from_event(event.event_id)
    assert result.from_state is TaskState.NEW
    assert result.to_state is TaskState.PLANNING
    assert result.skipped is False
    assert task is not None
    assert task.state is TaskState.PLANNING
    assert task.requested_by == "forgejo:alice"
    assert task.notification_target is None
    assert task.lease_branch is None
    assert task.feature_branch is None
    assert task.worktree_path is None
    assert task_row is not None
    assert task_row["state"] == TaskState.PLANNING.value
    assert task_row["state_rev"] >= 1
    assert [row["stream"] for row in stream_rows] == ["task_snapshot"]


def test_new_forgejo_issue_opened_seeds_lease_branch_from_payload(sqlite_conn) -> None:
    router = Router(Settings())
    event = _event(
        event_id="evt-opened-branch",
        event_type="issues.opened",
        payload={"source_issue_id": "84", "sender_login": "alice", "base_branch": "main"},
        source=Source.FORGEJO,
    )
    queries.insert_event(sqlite_conn, event)

    result = router.run_once(sqlite_conn, event)

    task = queries.get_task(sqlite_conn, result.task_id)
    assert task is not None
    assert task.lease_branch == "main"
    assert task.feature_branch is None
    assert task.worktree_path is None


def test_new_discord_issue_opened_sets_notification_target(sqlite_conn) -> None:
    router = Router(Settings())
    event = _event(
        event_id="evt-discord-opened",
        event_type="issues.opened",
        payload={"source_issue_id": "84", "actor": "123"},
        source=Source.DISCORD,
    )
    queries.insert_event(sqlite_conn, event)

    result = router.run_once(sqlite_conn, event)

    task = queries.get_task(sqlite_conn, result.task_id)
    assert task is not None
    assert task.requested_by == "discord:123"
    assert task.notification_target == "123"
    assert task.lease_branch is None
    assert task.feature_branch is None
    assert task.worktree_path is None


def test_planning_plan_ready_without_auto_ok_goes_pending_approval(sqlite_conn) -> None:
    router = Router(Settings())
    task = _seed_task(sqlite_conn, "task-1", state=TaskState.PLANNING)
    insert_plan_row(
        sqlite_conn,
        task_id=task.task_id,
        plan_rev=1,
        plan_json={"allowed_files": ["src/a.py"]},
        validator_score=70,
        created_at=task.created_at,
    )
    event = _event(
        event_id="evt-plan-ready",
        event_type="internal.plan_ready",
        payload={"task_id": task.task_id},
    )
    queries.insert_event(sqlite_conn, event)

    result = router.run_once(sqlite_conn, event)

    updated = queries.get_task(sqlite_conn, task.task_id)
    assert result.to_state is TaskState.PLAN_PENDING_APPROVAL
    assert updated is not None
    assert updated.state is TaskState.PLAN_PENDING_APPROVAL


def test_pending_approval_approve_goes_plan_approved(sqlite_conn) -> None:
    router = Router(Settings())
    task = _seed_task(sqlite_conn, "task-2", state=TaskState.PLAN_PENDING_APPROVAL)
    insert_plan_row(
        sqlite_conn,
        task_id=task.task_id,
        plan_rev=3,
        plan_json={"allowed_files": ["src/a.py"], "acceptance_criteria": ["works"]},
        created_at=task.created_at,
    )
    event = _event(
        event_id="evt-approve",
        event_type="/approve",
        payload={"task_id": task.task_id, "plan_rev": 3},
        source=Source.CLI,
    )
    queries.insert_event(sqlite_conn, event)

    result = router.run_once(sqlite_conn, event)

    updated = queries.get_task(sqlite_conn, task.task_id)
    assert result.to_state is TaskState.PLAN_APPROVED
    assert updated is not None
    assert updated.state is TaskState.PLAN_APPROVED


def test_plan_approved_critical_on_returns_to_pending_and_sets_flag(sqlite_conn) -> None:
    router = Router(Settings())
    task = _seed_task(sqlite_conn, "task-3", state=TaskState.PLAN_APPROVED)
    event = _event(
        event_id="evt-critical-on",
        event_type="/critical on",
        payload={"task_id": task.task_id},
        source=Source.CLI,
    )
    queries.insert_event(sqlite_conn, event)

    result = router.run_once(sqlite_conn, event)

    updated = queries.get_task(sqlite_conn, task.task_id)
    assert result.to_state is TaskState.PLAN_PENDING_APPROVAL
    assert updated is not None
    assert updated.state is TaskState.PLAN_PENDING_APPROVAL
    assert updated.critical is True


def test_implementing_lease_lost_goes_human_review_required_with_alert(sqlite_conn) -> None:
    router = Router(Settings())
    task = _seed_task(sqlite_conn, "task-4", state=TaskState.IMPLEMENTING)
    event = _event(
        event_id="evt-lease-lost",
        event_type="internal.lease_lost",
        payload={"task_id": task.task_id},
    )
    queries.insert_event(sqlite_conn, event)

    result = router.run_once(sqlite_conn, event)

    updated = queries.get_task(sqlite_conn, task.task_id)
    streams = sqlite_conn.execute(
        "SELECT stream FROM projection_outbox WHERE origin_event_id = ? ORDER BY outbox_id",
        (event.event_id,),
    ).fetchall()
    assert result.to_state is TaskState.HUMAN_REVIEW_REQUIRED
    assert updated is not None
    assert updated.state is TaskState.HUMAN_REVIEW_REQUIRED
    assert [row["stream"] for row in streams].count("discord_alert") == 1


def test_planner_timeout_goes_human_review_required(sqlite_conn) -> None:
    router = Router(Settings())
    task = _seed_task(sqlite_conn, "task-timeout-plan", state=TaskState.PLANNING)
    event = _event(
        event_id="evt-planner-timeout",
        event_type="internal.planner_timeout",
        payload={"task_id": task.task_id},
    )
    queries.insert_event(sqlite_conn, event)

    result = router.run_once(sqlite_conn, event)

    updated = queries.get_task(sqlite_conn, task.task_id)
    assert result.to_state is TaskState.HUMAN_REVIEW_REQUIRED
    assert updated is not None
    assert updated.state is TaskState.HUMAN_REVIEW_REQUIRED


def test_reviewer_timeout_goes_human_review_required(sqlite_conn) -> None:
    router = Router(Settings())
    task = _seed_task(sqlite_conn, "task-timeout-review", state=TaskState.REVIEWING)
    event = _event(
        event_id="evt-reviewer-timeout",
        event_type="internal.reviewer_timeout",
        payload={"task_id": task.task_id},
    )
    queries.insert_event(sqlite_conn, event)

    result = router.run_once(sqlite_conn, event)

    updated = queries.get_task(sqlite_conn, task.task_id)
    assert result.to_state is TaskState.HUMAN_REVIEW_REQUIRED
    assert updated is not None
    assert updated.state is TaskState.HUMAN_REVIEW_REQUIRED


def test_cancel_wildcard_cancels_non_done_task(sqlite_conn) -> None:
    router = Router(Settings())
    task = _seed_task(sqlite_conn, "task-5", state=TaskState.NEEDS_FIX)
    event = _event(
        event_id="evt-cancel",
        event_type="/cancel",
        payload={"task_id": task.task_id},
        source=Source.CLI,
    )
    queries.insert_event(sqlite_conn, event)

    result = router.run_once(sqlite_conn, event)

    updated = queries.get_task(sqlite_conn, task.task_id)
    assert result.to_state is TaskState.CANCELLED
    assert updated is not None
    assert updated.state is TaskState.CANCELLED
