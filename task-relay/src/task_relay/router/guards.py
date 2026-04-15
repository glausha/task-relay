from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from task_relay.clock import Clock
from task_relay.config import Settings
from task_relay.errors import FailureCode
from task_relay.types import InboxEvent, Plan, Task


@dataclass(frozen=True)
class GuardContext:
    task: Task
    event: InboxEvent
    latest_plan: Plan | None
    critical: bool
    settings: Settings
    clock: Clock
    conn: sqlite3.Connection


def _payload_bool(payload: dict[str, object], *keys: str) -> bool:
    for key in keys:
        if key in payload:
            return bool(payload[key])
    return False


def _unchecked_count(payload: dict[str, object]) -> int:
    if "unchecked" in payload:
        return int(payload["unchecked"])
    criteria = payload.get("criteria")
    if not isinstance(criteria, list):
        return 0
    return sum(1 for item in criteria if isinstance(item, dict) and item.get("status") == "unchecked")


def auto_approve_ok(ctx: GuardContext) -> bool:
    plan = ctx.latest_plan
    if plan is None:
        return False
    plan_json = plan.plan_json
    return (
        plan.validator_errors == 0
        and plan.validator_score >= 85
        and not ctx.critical
        and bool(plan_json.get("allowed_files") or plan_json.get("auto_allowed_patterns"))
        and bool(plan_json.get("acceptance_criteria"))
    )


def branch_lease_acquired(ctx: GuardContext) -> bool:
    return ctx.event.payload.get("lease_acquired") is True


def executor_in_scope(ctx: GuardContext) -> bool:
    return not ctx.event.payload.get("out_of_scope_files", [])


def executor_exit_zero(ctx: GuardContext) -> bool:
    return int(ctx.event.payload.get("exit_code", 1)) == 0


def reviewer_pass_all_clear(ctx: GuardContext) -> bool:
    return _unchecked_count(ctx.event.payload) == 0 and not bool(
        ctx.event.payload.get("manual_gate_required", ctx.task.manual_gate_required)
    )


def reviewer_pass_manual_gate(ctx: GuardContext) -> bool:
    return _unchecked_count(ctx.event.payload) == 0 and bool(
        ctx.event.payload.get("manual_gate_required", ctx.task.manual_gate_required)
    )


def resume_worktree_clean(ctx: GuardContext) -> bool:
    return _payload_bool(ctx.event.payload, "worktree_clean") or ctx.event.payload.get("worktree_status") == "clean"


def resume_worktree_dirty_ok(ctx: GuardContext) -> bool:
    payload = ctx.event.payload
    dirty = (
        payload.get("worktree_status") == "dirty"
        or _payload_bool(payload, "worktree_dirty")
        or payload.get("worktree_clean") is False
    )
    head_matches = _payload_bool(payload, "head_matches") or (
        payload.get("worktree_clean") is False and payload.get("last_known_head_commit") is not None
    )
    return (
        dirty
        and head_matches
        and _payload_bool(payload, "heartbeat_fresh")
        and plan_rev_matches(ctx, payload.get("plan_rev"))
        and not payload.get("out_of_scope_files", [])
    )


def resume_grace_ok(ctx: GuardContext) -> bool:
    payload = ctx.event.payload
    if "resume_grace_ok" in payload:
        return bool(payload["resume_grace_ok"])
    if "within_grace" in payload:
        return bool(payload["within_grace"])
    grace_deadline_at = payload.get("grace_deadline_at")
    if isinstance(grace_deadline_at, str):
        return ctx.event.received_at.isoformat() <= grace_deadline_at
    return False


def plan_rev_matches(ctx: GuardContext, plan_rev: object) -> bool:
    if ctx.latest_plan is None or plan_rev is None:
        return False
    return ctx.latest_plan.plan_rev == int(plan_rev)


def critical_off(ctx: GuardContext) -> bool:
    payload = ctx.event.payload
    actor = payload.get("requested_by") or payload.get("actor")
    if actor == ctx.task.requested_by:
        return True
    actor_user_id = payload.get("actor_user_id") or payload.get("user_id") or payload.get("actor_id")
    try:
        return int(actor_user_id) in ctx.settings.admin_user_ids
    except (TypeError, ValueError):
        return False


def replan_required(ctx: GuardContext) -> bool:
    return bool(ctx.event.payload.get("replan_required"))


def validator_over(ctx: GuardContext) -> bool:
    return bool(ctx.event.payload.get("validator_over", True))


def executor_non_infra_error(ctx: GuardContext) -> bool:
    failure_code = ctx.event.payload.get("failure_code")
    return (
        not executor_exit_zero(ctx)
        and failure_code != FailureCode.SYSTEM_DEGRADED.value
        and failure_code != FailureCode.SYSTEM_DEGRADED
    )


def manual_gate_approved(ctx: GuardContext) -> bool:
    return ctx.task.manual_gate_required and _payload_bool(
        ctx.event.payload,
        "reviewed",
        "manual_gate_approved",
    )


def critical_is_false(ctx: GuardContext) -> bool:
    return not ctx.critical


def resume_target_present(ctx: GuardContext) -> bool:
    return ctx.task.resume_target_state is not None
