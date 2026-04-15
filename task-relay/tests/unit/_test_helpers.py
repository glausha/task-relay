from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from task_relay.types import Task, TaskState


def seed_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    created_at: datetime,
    source_issue_id: str | None = "42",
    state: TaskState = TaskState.NEW,
    state_rev: int = 0,
    critical: bool = False,
    current_branch: str | None = None,
    manual_gate_required: bool = False,
    last_known_head_commit: str | None = None,
    resume_target_state: TaskState | None = None,
    requested_by: str = "alice",
    updated_at: datetime | None = None,
) -> Task:
    task = Task(
        task_id=task_id,
        source_issue_id=source_issue_id,
        state=state,
        state_rev=state_rev,
        critical=critical,
        current_branch=current_branch,
        manual_gate_required=manual_gate_required,
        last_known_head_commit=last_known_head_commit,
        resume_target_state=resume_target_state,
        requested_by=requested_by,
        created_at=created_at,
        updated_at=created_at if updated_at is None else updated_at,
    )
    conn.execute(
        """
        INSERT INTO tasks(
            task_id, source_issue_id, state, state_rev, critical, current_branch,
            manual_gate_required, last_known_head_commit, resume_target_state,
            requested_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.task_id,
            task.source_issue_id,
            task.state.value,
            task.state_rev,
            int(task.critical),
            task.current_branch,
            int(task.manual_gate_required),
            task.last_known_head_commit,
            None if task.resume_target_state is None else task.resume_target_state.value,
            task.requested_by,
            task.created_at.isoformat(),
            task.updated_at.isoformat(),
        ),
    )
    return task


def insert_plan_row(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    plan_rev: int,
    plan_json: dict[str, object],
    validator_score: int = 90,
    validator_errors: int = 0,
    created_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO plans(
            task_id, plan_rev, planner_version, plan_json, validator_score,
            validator_errors, approved_by, approved_at, approved_kind, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            plan_rev,
            "planner-v1",
            json.dumps(plan_json, separators=(",", ":"), ensure_ascii=False),
            validator_score,
            validator_errors,
            None,
            None,
            None,
            created_at.isoformat(),
        ),
    )
