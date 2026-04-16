from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from importlib import import_module

from task_relay.projection.labels import MANAGED_LABELS
from task_relay.router.guards import GuardContext, resume_grace_ok
from task_relay.router.idempotency import discord_alert_key, label_sync_key, snapshot_key
from task_relay.types import AlertKind, BranchWaiterStatus, Severity, Stream, TaskState

_UNSET = object()
_ADMIN_USER_IDS_SENTINEL = "admin_user_ids"


def _queries():
    return import_module("task_relay.db.queries")


def requeue_branch_head_waiter(conn: sqlite3.Connection, branch: str) -> str | None:
    row = conn.execute(
        """
        SELECT task_id, status
        FROM branch_waiters
        WHERE branch = ?
          AND status IN (?, ?)
        ORDER BY queue_order ASC
        LIMIT 1
        """,
        (
            branch,
            BranchWaiterStatus.LEASED.value,
            BranchWaiterStatus.QUEUED.value,
        ),
    ).fetchone()
    if row is None:
        return None
    task_id = str(row["task_id"])
    if str(row["status"]) == BranchWaiterStatus.LEASED.value:
        # WHY: admin unlock must make the oldest waiter dispatchable again without rewinding tokens.
        conn.execute(
            """
            UPDATE branch_waiters
            SET status = ?
            WHERE branch = ? AND task_id = ?
            """,
            (BranchWaiterStatus.QUEUED.value, branch, task_id),
        )
    return task_id


def _issue_target(ctx: GuardContext) -> str:
    return ctx.task.source_issue_id or ctx.task.task_id


def _discord_target(ctx: GuardContext) -> str:
    # Keep the outbox target deterministic when sink-side admin fanout is required.
    return ctx.task.notification_target or _ADMIN_USER_IDS_SENTINEL


def _task_url(ctx: GuardContext) -> str:
    if ctx.task.source_issue_id:
        return f"{ctx.settings.forgejo_base_url}/issues/{ctx.task.source_issue_id}"
    return f"{ctx.settings.forgejo_base_url}/tasks/{ctx.task.task_id}"


def _next_attempt_at(ctx: GuardContext) -> str:
    return (ctx.event.received_at + timedelta(seconds=ctx.settings.projection_retry_initial_seconds)).isoformat()


def _snapshot_payload(ctx: GuardContext, *, state: TaskState, state_rev: int, critical: bool) -> dict[str, object]:
    latest_plan_rev = ctx.latest_plan.plan_rev if ctx.latest_plan is not None else None
    return {
        "state": state.value,
        "state_rev": state_rev,
        "plan_rev": latest_plan_rev,
        "critical": critical,
        "task_url": _task_url(ctx),
    }


def _desired_labels(state: TaskState, *, critical: bool) -> list[str]:
    labels: set[str] = set()
    if critical:
        labels.add("critical")
    if state is TaskState.HUMAN_REVIEW_REQUIRED:
        labels.add("human_review_required")
    if state is TaskState.CANCELLED:
        labels.add("cancelled")
    return sorted(labels)


def bump_state(
    ctx: GuardContext,
    new_state: TaskState,
    *,
    reason: str | None = None,
    critical: bool | None = None,
    manual_gate_required: bool | None = None,
    resume_target_state: TaskState | None | object = _UNSET,
    lease_branch: str | None | object = _UNSET,
    feature_branch: str | None | object = _UNSET,
    worktree_path: str | None | object = _UNSET,
) -> tuple[int, bool]:
    queries = _queries()
    state_rev = ctx.task.state_rev + 1
    critical_value = ctx.task.critical if critical is None else critical
    update_kwargs: dict[str, object] = {
        "task_id": ctx.task.task_id,
        "new_state": new_state,
        "new_state_rev": state_rev,
        "updated_at": ctx.event.received_at,
    }
    if critical is not None:
        update_kwargs["critical"] = critical
    if manual_gate_required is not None:
        update_kwargs["manual_gate_required"] = manual_gate_required
    if resume_target_state is not _UNSET:
        update_kwargs["resume_target_state"] = resume_target_state
    if lease_branch is not _UNSET:
        update_kwargs["lease_branch"] = lease_branch
    if feature_branch is not _UNSET:
        update_kwargs["feature_branch"] = feature_branch
    if worktree_path is not _UNSET:
        update_kwargs["worktree_path"] = worktree_path
    queries.update_task_state(ctx.conn, **update_kwargs)
    queries.insert_system_event(
        ctx.conn,
        task_id=ctx.task.task_id,
        event_type="state_changed",
        severity=Severity.INFO,
        payload_json=json.dumps(
            {"from": ctx.task.state.value, "to": new_state.value, "reason": reason or ctx.event.event_type},
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        created_at=ctx.event.received_at,
    )
    return state_rev, bool(critical_value)


def _insert_snapshot(ctx: GuardContext, *, state: TaskState, state_rev: int, critical: bool) -> int:
    queries = _queries()
    target = _issue_target(ctx)
    payload = _snapshot_payload(ctx, state=state, state_rev=state_rev, critical=critical)
    return queries.insert_outbox(
        ctx.conn,
        task_id=ctx.task.task_id,
        stream=Stream.TASK_SNAPSHOT,
        target=target,
        origin_event_id=ctx.event.event_id,
        payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        state_rev=state_rev,
        idempotency_key=snapshot_key(ctx.task.task_id, target, state_rev, payload),
        next_attempt_at=_next_attempt_at(ctx),
    )


def _insert_label_sync(ctx: GuardContext, *, state: TaskState, state_rev: int, critical: bool) -> int:
    queries = _queries()
    target = _issue_target(ctx)
    desired_labels = _desired_labels(state, critical=critical)
    payload = {
        "desired_labels": desired_labels,
        "managed_labels": sorted(MANAGED_LABELS),
    }
    return queries.insert_outbox(
        ctx.conn,
        task_id=ctx.task.task_id,
        stream=Stream.TASK_LABEL_SYNC,
        target=target,
        origin_event_id=ctx.event.event_id,
        payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        state_rev=state_rev,
        idempotency_key=label_sync_key(ctx.task.task_id, target, state_rev, desired_labels),
        next_attempt_at=_next_attempt_at(ctx),
    )


def _insert_alert(ctx: GuardContext, *, alert_kind: AlertKind, state: TaskState, state_rev: int) -> int:
    queries = _queries()
    target = _discord_target(ctx)
    payload = {
        "kind": alert_kind.value,
        "state": state.value,
        "task_id": ctx.task.task_id,
        "task_url": _task_url(ctx),
    }
    return queries.insert_outbox(
        ctx.conn,
        task_id=ctx.task.task_id,
        stream=Stream.DISCORD_ALERT,
        target=target,
        origin_event_id=ctx.event.event_id,
        payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        state_rev=state_rev,
        idempotency_key=discord_alert_key(ctx.task.task_id, target, alert_kind.value, state_rev),
        next_attempt_at=_next_attempt_at(ctx),
    )


def apply_new_to_planning(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.PLANNING)
    _insert_snapshot(ctx, state=TaskState.PLANNING, state_rev=state_rev, critical=critical)


def apply_plan_ready_auto_approved(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.PLAN_APPROVED)
    _insert_snapshot(ctx, state=TaskState.PLAN_APPROVED, state_rev=state_rev, critical=critical)


def apply_plan_ready_pending_approval(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.PLAN_PENDING_APPROVAL)
    _insert_snapshot(ctx, state=TaskState.PLAN_PENDING_APPROVAL, state_rev=state_rev, critical=critical)


def apply_plan_approved(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.PLAN_APPROVED)
    _insert_snapshot(ctx, state=TaskState.PLAN_APPROVED, state_rev=state_rev, critical=critical)


def apply_dispatch_to_implementing(ctx: GuardContext) -> None:
    lease_branch = ctx.event.payload.get("lease_branch") or ctx.task.lease_branch
    feature_branch = ctx.event.payload.get("feature_branch")
    worktree_path = ctx.event.payload.get("worktree_path")
    state_rev, critical = bump_state(
        ctx,
        TaskState.IMPLEMENTING,
        lease_branch=None if lease_branch is None else str(lease_branch),
        feature_branch=None if feature_branch is None else str(feature_branch),
        worktree_path=None if worktree_path is None else str(worktree_path),
    )
    _insert_snapshot(ctx, state=TaskState.IMPLEMENTING, state_rev=state_rev, critical=critical)


def apply_dispatch_deferred(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.PLAN_APPROVED)
    _insert_snapshot(ctx, state=TaskState.PLAN_APPROVED, state_rev=state_rev, critical=critical)


def apply_implementing_to_reviewing(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.REVIEWING)
    _insert_snapshot(ctx, state=TaskState.REVIEWING, state_rev=state_rev, critical=critical)


def apply_done(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.DONE)
    _insert_snapshot(ctx, state=TaskState.DONE, state_rev=state_rev, critical=critical)
    _insert_label_sync(ctx, state=TaskState.DONE, state_rev=state_rev, critical=critical)


def apply_done_after_manual_gate(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.DONE, manual_gate_required=False)
    _insert_snapshot(ctx, state=TaskState.DONE, state_rev=state_rev, critical=critical)
    _insert_label_sync(ctx, state=TaskState.DONE, state_rev=state_rev, critical=critical)


def apply_system_degraded(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.SYSTEM_DEGRADED, resume_target_state=ctx.task.state)
    _insert_snapshot(ctx, state=TaskState.SYSTEM_DEGRADED, state_rev=state_rev, critical=critical)
    _insert_alert(ctx, alert_kind=AlertKind.SYSTEM_DEGRADED, state=TaskState.SYSTEM_DEGRADED, state_rev=state_rev)


def apply_human_review_required(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.HUMAN_REVIEW_REQUIRED)
    _insert_alert(
        ctx,
        alert_kind=AlertKind.HUMAN_REVIEW_REQUIRED,
        state=TaskState.HUMAN_REVIEW_REQUIRED,
        state_rev=state_rev,
    )
    _insert_label_sync(ctx, state=TaskState.HUMAN_REVIEW_REQUIRED, state_rev=state_rev, critical=critical)


def apply_cancelled(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.CANCELLED)
    _insert_label_sync(ctx, state=TaskState.CANCELLED, state_rev=state_rev, critical=critical)


def apply_retry_to_planning(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.PLANNING)
    _insert_snapshot(ctx, state=TaskState.PLANNING, state_rev=state_rev, critical=critical)


def apply_critical_on_same_state(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, ctx.task.state, critical=True)
    _insert_snapshot(ctx, state=ctx.task.state, state_rev=state_rev, critical=critical)
    _insert_label_sync(ctx, state=ctx.task.state, state_rev=state_rev, critical=critical)


def apply_critical_on_manual_gate(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, ctx.task.state, critical=True, manual_gate_required=True)
    _insert_snapshot(ctx, state=ctx.task.state, state_rev=state_rev, critical=critical)
    _insert_label_sync(ctx, state=ctx.task.state, state_rev=state_rev, critical=critical)


def apply_critical_on_reapproval(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.PLAN_PENDING_APPROVAL, critical=True)
    _insert_snapshot(ctx, state=TaskState.PLAN_PENDING_APPROVAL, state_rev=state_rev, critical=critical)
    _insert_label_sync(ctx, state=TaskState.PLAN_PENDING_APPROVAL, state_rev=state_rev, critical=critical)


def apply_critical_off_same_state(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, ctx.task.state, critical=False)
    _insert_snapshot(ctx, state=ctx.task.state, state_rev=state_rev, critical=critical)
    _insert_label_sync(ctx, state=ctx.task.state, state_rev=state_rev, critical=critical)


def apply_needs_fix(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.NEEDS_FIX)
    _insert_snapshot(ctx, state=TaskState.NEEDS_FIX, state_rev=state_rev, critical=critical)


def apply_resume_clean_to_plan_approved(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.PLAN_APPROVED)
    _insert_snapshot(ctx, state=TaskState.PLAN_APPROVED, state_rev=state_rev, critical=critical)


def apply_resume_pending(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.IMPLEMENTING_RESUME_PENDING)
    _insert_snapshot(ctx, state=TaskState.IMPLEMENTING_RESUME_PENDING, state_rev=state_rev, critical=critical)


def apply_resume_grace_continue(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.IMPLEMENTING)
    _insert_snapshot(ctx, state=TaskState.IMPLEMENTING, state_rev=state_rev, critical=critical)


def apply_retry_to_implementing(ctx: GuardContext) -> None:
    state_rev, critical = bump_state(ctx, TaskState.IMPLEMENTING)
    _insert_snapshot(ctx, state=TaskState.IMPLEMENTING, state_rev=state_rev, critical=critical)


def resume_recovered_state(ctx: GuardContext) -> TaskState:
    target = ctx.task.resume_target_state
    if target is None:
        return TaskState.HUMAN_REVIEW_REQUIRED
    if target is TaskState.IMPLEMENTING:
        return TaskState.PLAN_APPROVED
    if target is TaskState.IMPLEMENTING_RESUME_PENDING:
        return TaskState.IMPLEMENTING if resume_grace_ok(ctx) else TaskState.HUMAN_REVIEW_REQUIRED
    if target in {TaskState.DONE, TaskState.CANCELLED, TaskState.SYSTEM_DEGRADED}:
        return TaskState.HUMAN_REVIEW_REQUIRED
    return target


def apply_system_recovered(ctx: GuardContext) -> None:
    new_state = resume_recovered_state(ctx)
    if new_state is TaskState.HUMAN_REVIEW_REQUIRED:
        state_rev, critical = bump_state(
            ctx,
            TaskState.HUMAN_REVIEW_REQUIRED,
            reason="internal.system_recovered",
            resume_target_state=None,
        )
        _insert_alert(
            ctx,
            alert_kind=AlertKind.HUMAN_REVIEW_REQUIRED,
            state=TaskState.HUMAN_REVIEW_REQUIRED,
            state_rev=state_rev,
        )
        _insert_label_sync(ctx, state=TaskState.HUMAN_REVIEW_REQUIRED, state_rev=state_rev, critical=critical)
        return
    state_rev, critical = bump_state(
        ctx,
        new_state,
        reason="internal.system_recovered",
        resume_target_state=None,
    )
    _insert_snapshot(ctx, state=new_state, state_rev=state_rev, critical=critical)
