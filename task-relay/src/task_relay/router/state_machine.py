"""
正常系:
  (new, accept)                      -> planning
  (planning, plan_ready & auto_ok)   -> plan_approved
  (planning, plan_ready & !auto_ok)  -> plan_pending_approval
  (plan_pending_approval, /approve)  -> plan_approved
  (plan_approved, dispatch & leased) -> implementing
  (plan_approved, dispatch & !lease) -> plan_approved (self)
  (implementing, executor_finished & in_scope & exit=0) -> reviewing
  (reviewing, reviewer_pass & unchecked=0 & !manual_gate) -> done
  (reviewing, reviewer_pass & unchecked=0 &  manual_gate) -> human_review_required

例外系:
  (planning, validator_over)              -> human_review_required
  (planning, infra_fatal/breaker)         -> system_degraded
  (plan_pending_approval, /critical on)   -> plan_pending_approval (flag only)
  (plan_pending_approval, /retry --replan)-> planning
  (plan_pending_approval, /cancel)        -> cancelled
  (plan_approved, /critical on)           -> plan_pending_approval
  (implementing, executor_finished & out_of_scope) -> needs_fix
  (implementing, executor_finished & non_infra_err) -> needs_fix
  (implementing, reconcile_resume & dirty & ok)     -> implementing_resume_pending
  (implementing, lease_lost)             -> human_review_required
  (implementing, infra_fatal/breaker)    -> system_degraded
  (implementing_resume_pending, resume_grace_ok)    -> implementing
  (implementing_resume_pending, resume_grace_expired) -> human_review_required
  (needs_fix, /retry)                    -> implementing
  (needs_fix, /retry --replan | replan_required) -> planning
  (reviewing, reviewer_fail)             -> needs_fix
  (reviewing, reviewer_human_review)     -> human_review_required
  (human_review_required, /approve & reviewed & manual_gate) -> done
  (human_review_required, /retry)        -> implementing
  (human_review_required, /retry --replan) -> planning
  (system_degraded, system_recovered)    -> resume_target_state (re-evaluate guard)
  (*, /cancel) where state != done       -> cancelled

internal event types: internal.executor_finished, internal.planner_timeout, internal.reviewer_timeout, internal.lease_lost, internal.infra_fatal, internal.reconcile_resume, internal.system_recovered, internal.unlock_requested
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from task_relay.router import transitions
from task_relay.router.guards import (
    GuardContext,
    auto_approve_ok,
    branch_lease_acquired,
    critical_is_false,
    critical_off,
    executor_exit_zero,
    executor_in_scope,
    executor_non_infra_error,
    manual_gate_approved,
    plan_rev_matches,
    replan_required,
    resume_grace_ok,
    resume_target_present,
    resume_worktree_clean,
    resume_worktree_dirty_ok,
    reviewer_pass_all_clear,
    reviewer_pass_manual_gate,
    validator_over,
)
from task_relay.types import TaskState


@dataclass(frozen=True)
class TransitionKey:
    state: TaskState
    event_type: str


@dataclass
class TransitionSpec:
    guard: Callable[[GuardContext], bool]
    to_state_fn: Callable[[GuardContext], TaskState]
    on_apply: Callable[[GuardContext], None] | None = None


def _always(_ctx: GuardContext) -> bool:
    return True


def _to(state: TaskState) -> Callable[[GuardContext], TaskState]:
    return lambda _ctx: state


def _matches_current_plan(ctx: GuardContext) -> bool:
    return plan_rev_matches(ctx, ctx.event.payload.get("plan_rev"))


def _resume_recovered_to_state(ctx: GuardContext) -> TaskState:
    return transitions.resume_recovered_state(ctx)


def _add(
    table: dict[TransitionKey, list[TransitionSpec]],
    state: TaskState,
    event_type: str,
    *specs: TransitionSpec,
) -> None:
    table[TransitionKey(state=state, event_type=event_type)] = list(specs)


def apply_cancel_any(state: TaskState) -> tuple[TransitionKey, list[TransitionSpec]]:
    return (
        TransitionKey(state=state, event_type="/cancel"),
        [TransitionSpec(_always, _to(TaskState.CANCELLED), transitions.apply_cancelled)],
    )


TRANSITIONS: dict[TransitionKey, list[TransitionSpec]] = {}

_add(TRANSITIONS, TaskState.NEW, "issues.opened", TransitionSpec(_always, _to(TaskState.PLANNING), transitions.apply_new_to_planning))
_add(TRANSITIONS, TaskState.NEW, "/critical on", TransitionSpec(_always, _to(TaskState.NEW), transitions.apply_critical_on_same_state))
_add(TRANSITIONS, TaskState.NEW, "/critical off", TransitionSpec(critical_off, _to(TaskState.NEW), transitions.apply_critical_off_same_state))

_add(
    TRANSITIONS,
    TaskState.PLANNING,
    "internal.plan_ready",
    TransitionSpec(auto_approve_ok, _to(TaskState.PLAN_APPROVED), transitions.apply_plan_ready_auto_approved),
    TransitionSpec(_always, _to(TaskState.PLAN_PENDING_APPROVAL), transitions.apply_plan_ready_pending_approval),
)
_add(
    TRANSITIONS,
    TaskState.PLANNING,
    "internal.validator_over",
    TransitionSpec(validator_over, _to(TaskState.HUMAN_REVIEW_REQUIRED), transitions.apply_human_review_required),
)
_add(
    TRANSITIONS,
    TaskState.PLANNING,
    "internal.infra_fatal",
    TransitionSpec(_always, _to(TaskState.SYSTEM_DEGRADED), transitions.apply_system_degraded),
)
_add(
    TRANSITIONS,
    TaskState.PLANNING,
    "internal.planner_timeout",
    TransitionSpec(_always, _to(TaskState.HUMAN_REVIEW_REQUIRED), transitions.apply_human_review_required),
)
_add(TRANSITIONS, TaskState.PLANNING, "/critical on", TransitionSpec(_always, _to(TaskState.PLANNING), transitions.apply_critical_on_same_state))
_add(TRANSITIONS, TaskState.PLANNING, "/critical off", TransitionSpec(critical_off, _to(TaskState.PLANNING), transitions.apply_critical_off_same_state))

_add(
    TRANSITIONS,
    TaskState.PLAN_PENDING_APPROVAL,
    "/approve",
    TransitionSpec(_matches_current_plan, _to(TaskState.PLAN_APPROVED), transitions.apply_plan_approved),
)
_add(
    TRANSITIONS,
    TaskState.PLAN_PENDING_APPROVAL,
    "/critical on",
    TransitionSpec(critical_is_false, _to(TaskState.PLAN_PENDING_APPROVAL), transitions.apply_critical_on_same_state),
)
_add(
    TRANSITIONS,
    TaskState.PLAN_PENDING_APPROVAL,
    "/critical off",
    TransitionSpec(critical_off, _to(TaskState.PLAN_PENDING_APPROVAL), transitions.apply_critical_off_same_state),
)
_add(
    TRANSITIONS,
    TaskState.PLAN_PENDING_APPROVAL,
    "/retry --replan",
    TransitionSpec(_always, _to(TaskState.PLANNING), transitions.apply_retry_to_planning),
)

_add(
    TRANSITIONS,
    TaskState.PLAN_APPROVED,
    "internal.dispatch_attempt",
    TransitionSpec(branch_lease_acquired, _to(TaskState.IMPLEMENTING), transitions.apply_dispatch_to_implementing),
    TransitionSpec(_always, _to(TaskState.PLAN_APPROVED), transitions.apply_dispatch_deferred),
)
_add(
    TRANSITIONS,
    TaskState.PLAN_APPROVED,
    "/critical on",
    TransitionSpec(_always, _to(TaskState.PLAN_PENDING_APPROVAL), transitions.apply_critical_on_reapproval),
)
_add(
    TRANSITIONS,
    TaskState.PLAN_APPROVED,
    "/critical off",
    TransitionSpec(critical_off, _to(TaskState.PLAN_APPROVED), transitions.apply_critical_off_same_state),
)

_add(
    TRANSITIONS,
    TaskState.IMPLEMENTING,
    "internal.executor_finished",
    TransitionSpec(lambda ctx: executor_exit_zero(ctx) and executor_in_scope(ctx), _to(TaskState.REVIEWING), transitions.apply_implementing_to_reviewing),
    TransitionSpec(lambda ctx: not executor_in_scope(ctx), _to(TaskState.NEEDS_FIX), transitions.apply_needs_fix),
    TransitionSpec(executor_non_infra_error, _to(TaskState.NEEDS_FIX), transitions.apply_needs_fix),
)
_add(
    TRANSITIONS,
    TaskState.IMPLEMENTING,
    "internal.reconcile_resume",
    TransitionSpec(resume_worktree_clean, _to(TaskState.PLAN_APPROVED), transitions.apply_resume_clean_to_plan_approved),
    TransitionSpec(resume_worktree_dirty_ok, _to(TaskState.IMPLEMENTING_RESUME_PENDING), transitions.apply_resume_pending),
    TransitionSpec(_always, _to(TaskState.HUMAN_REVIEW_REQUIRED), transitions.apply_human_review_required),
)
_add(
    TRANSITIONS,
    TaskState.IMPLEMENTING,
    "internal.lease_lost",
    TransitionSpec(_always, _to(TaskState.HUMAN_REVIEW_REQUIRED), transitions.apply_human_review_required),
)
_add(
    TRANSITIONS,
    TaskState.IMPLEMENTING,
    "internal.infra_fatal",
    TransitionSpec(_always, _to(TaskState.SYSTEM_DEGRADED), transitions.apply_system_degraded),
)
_add(
    TRANSITIONS,
    TaskState.IMPLEMENTING,
    "/critical on",
    TransitionSpec(_always, _to(TaskState.IMPLEMENTING), transitions.apply_critical_on_manual_gate),
)
_add(
    TRANSITIONS,
    TaskState.IMPLEMENTING,
    "/critical off",
    TransitionSpec(critical_off, _to(TaskState.IMPLEMENTING), transitions.apply_critical_off_same_state),
)

_add(
    TRANSITIONS,
    TaskState.IMPLEMENTING_RESUME_PENDING,
    "internal.reconcile_resume",
    TransitionSpec(resume_grace_ok, _to(TaskState.IMPLEMENTING), transitions.apply_resume_grace_continue),
    TransitionSpec(_always, _to(TaskState.HUMAN_REVIEW_REQUIRED), transitions.apply_human_review_required),
)

_add(
    TRANSITIONS,
    TaskState.NEEDS_FIX,
    "/retry",
    TransitionSpec(lambda ctx: not replan_required(ctx), _to(TaskState.IMPLEMENTING), transitions.apply_retry_to_implementing),
)
_add(
    TRANSITIONS,
    TaskState.NEEDS_FIX,
    "/retry --replan",
    TransitionSpec(_always, _to(TaskState.PLANNING), transitions.apply_retry_to_planning),
)
_add(
    TRANSITIONS,
    TaskState.NEEDS_FIX,
    "/critical off",
    TransitionSpec(critical_off, _to(TaskState.NEEDS_FIX), transitions.apply_critical_off_same_state),
)

_add(
    TRANSITIONS,
    TaskState.REVIEWING,
    "internal.reviewer_pass",
    TransitionSpec(reviewer_pass_all_clear, _to(TaskState.DONE), transitions.apply_done),
    TransitionSpec(reviewer_pass_manual_gate, _to(TaskState.HUMAN_REVIEW_REQUIRED), transitions.apply_human_review_required),
)
_add(TRANSITIONS, TaskState.REVIEWING, "internal.reviewer_fail", TransitionSpec(_always, _to(TaskState.NEEDS_FIX), transitions.apply_needs_fix))
_add(
    TRANSITIONS,
    TaskState.REVIEWING,
    "internal.reviewer_human_review",
    TransitionSpec(_always, _to(TaskState.HUMAN_REVIEW_REQUIRED), transitions.apply_human_review_required),
)
_add(
    TRANSITIONS,
    TaskState.REVIEWING,
    "internal.reviewer_timeout",
    TransitionSpec(_always, _to(TaskState.HUMAN_REVIEW_REQUIRED), transitions.apply_human_review_required),
)
_add(TRANSITIONS, TaskState.REVIEWING, "/critical on", TransitionSpec(_always, _to(TaskState.REVIEWING), transitions.apply_critical_on_manual_gate))
_add(TRANSITIONS, TaskState.REVIEWING, "/critical off", TransitionSpec(critical_off, _to(TaskState.REVIEWING), transitions.apply_critical_off_same_state))

_add(
    TRANSITIONS,
    TaskState.HUMAN_REVIEW_REQUIRED,
    "/approve",
    TransitionSpec(manual_gate_approved, _to(TaskState.DONE), transitions.apply_done_after_manual_gate),
)
_add(
    TRANSITIONS,
    TaskState.HUMAN_REVIEW_REQUIRED,
    "/retry",
    TransitionSpec(lambda ctx: not replan_required(ctx), _to(TaskState.IMPLEMENTING), transitions.apply_retry_to_implementing),
)
_add(
    TRANSITIONS,
    TaskState.HUMAN_REVIEW_REQUIRED,
    "/retry --replan",
    TransitionSpec(_always, _to(TaskState.PLANNING), transitions.apply_retry_to_planning),
)
_add(
    TRANSITIONS,
    TaskState.HUMAN_REVIEW_REQUIRED,
    "/critical off",
    TransitionSpec(critical_off, _to(TaskState.HUMAN_REVIEW_REQUIRED), transitions.apply_critical_off_same_state),
)

_add(
    TRANSITIONS,
    TaskState.SYSTEM_DEGRADED,
    "internal.system_recovered",
    TransitionSpec(resume_target_present, _resume_recovered_to_state, transitions.apply_system_recovered),
)
_add(
    TRANSITIONS,
    TaskState.SYSTEM_DEGRADED,
    "/critical off",
    TransitionSpec(critical_off, _to(TaskState.SYSTEM_DEGRADED), transitions.apply_critical_off_same_state),
)
_add(
    TRANSITIONS,
    TaskState.CANCELLED,
    "/critical off",
    TransitionSpec(critical_off, _to(TaskState.CANCELLED), transitions.apply_critical_off_same_state),
)

for _state in TaskState:
    if _state is not TaskState.DONE:
        _key, _value = apply_cancel_any(_state)
        TRANSITIONS[_key] = _value
