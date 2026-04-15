from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class TaskState(str, Enum):
    NEW = "new"
    PLANNING = "planning"
    PLAN_PENDING_APPROVAL = "plan_pending_approval"
    PLAN_APPROVED = "plan_approved"
    IMPLEMENTING = "implementing"
    IMPLEMENTING_RESUME_PENDING = "implementing_resume_pending"
    NEEDS_FIX = "needs_fix"
    REVIEWING = "reviewing"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    DONE = "done"
    CANCELLED = "cancelled"
    SYSTEM_DEGRADED = "system_degraded"


class Source(str, Enum):
    FORGEJO = "forgejo"
    DISCORD = "discord"
    CLI = "cli"
    INTERNAL = "internal"


class Stream(str, Enum):
    TASK_SNAPSHOT = "task_snapshot"
    TASK_COMMENT = "task_comment"
    TASK_LABEL_SYNC = "task_label_sync"
    DISCORD_ALERT = "discord_alert"


class BranchWaiterStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    PAUSED_SYSTEM_DEGRADED = "paused_system_degraded"
    REMOVED = "removed"


class ApprovedKind(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


class SystemEventType(str, Enum):
    MIRROR_READONLY_VIOLATION_DETECTED = "mirror_readonly_violation_detected"
    RETENTION_ORPHAN_DETECTED = "retention_orphan_detected"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Stage(str, Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    REVIEWING = "reviewing"


class CommentKind(str, Enum):
    AUDIT = "audit"
    SYSTEM = "system"


class AlertKind(str, Enum):
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    SYSTEM_DEGRADED = "system_degraded"
    BREAKER_OPEN = "breaker_open"


class PolicyBreach(str, Enum):
    TOUCHED_FORBIDDEN_FILE = "touched_forbidden_file"
    TOUCHED_OUT_OF_SCOPE_FILE = "touched_out_of_scope_file"
    MISSING_TEST = "missing_test"
    ACCEPTANCE_NOT_MET = "acceptance_not_met"
    LEASE_ASSERT_MISSING = "lease_assert_missing"
    UNEXPECTED_GENERATED_FILE = "unexpected_generated_file"


class ReviewDecision(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    HUMAN_REVIEW_REQUIRED = "human_review_required"


class CriterionStatus(str, Enum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    UNCHECKED = "unchecked"


@dataclass(frozen=True)
class AdapterContract:
    name: str
    version: str
    supports_request_id: bool


@dataclass(frozen=True)
class CanonicalEvent:
    event_id: str
    source: Source
    delivery_id: str
    event_type: str
    payload: dict[str, Any]
    received_at: datetime
    request_id: str | None


@dataclass(frozen=True)
class JournalPosition:
    file: str
    offset: int


@dataclass(frozen=True)
class InboxEvent:
    event_id: str
    source: Source
    delivery_id: str
    event_type: str
    payload: dict[str, Any]
    journal_offset: int
    received_at: datetime


@dataclass(frozen=True)
class OutboxRecord:
    outbox_id: int
    task_id: str
    stream: Stream
    target: str
    origin_event_id: str
    payload: dict[str, Any]
    state_rev: int
    idempotency_key: str
    attempt_count: int
    next_attempt_at: datetime
    sent_at: datetime | None


@dataclass(frozen=True)
class Task:
    task_id: str
    source_issue_id: str | None
    state: TaskState
    state_rev: int
    critical: bool
    current_branch: str | None
    manual_gate_required: bool
    last_known_head_commit: str | None
    resume_target_state: TaskState | None
    requested_by: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Plan:
    task_id: str
    plan_rev: int
    planner_version: str
    plan_json: dict[str, Any]
    validator_score: int
    validator_errors: int
    approved_by: str | None
    approved_at: datetime | None
    approved_kind: ApprovedKind | None
    created_at: datetime


@dataclass(frozen=True)
class ToolCallRecord:
    call_id: str
    task_id: str
    stage: Stage
    tool_name: str
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None
    success: bool | None
    exit_code: int | None
    failure_code: str | None
    log_path: str | None
    log_sha256: str | None
    log_bytes: int | None
    tokens_in: int | None
    tokens_out: int | None


@dataclass(frozen=True)
class RateWindow:
    tool_name: str
    window_started_at: datetime
    window_reset_at: datetime
    remaining: int
    limit: int
    updated_at: datetime
