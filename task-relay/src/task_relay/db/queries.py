"""Pure SQLite query helpers for detailed-design §2."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from task_relay.db.connection import fetch_one, tx
from task_relay.types import (
    ApprovedKind,
    BranchWaiterStatus,
    InboxEvent,
    OutboxRecord,
    Plan,
    RateWindow,
    Severity,
    Source,
    Stage,
    Stream,
    Task,
    TaskState,
    ToolCallRecord,
)


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    row = fetch_one(
        conn,
        """
        SELECT task_id, source_issue_id, state, state_rev, critical, current_branch,
               manual_gate_required, last_known_head_commit, resume_target_state,
               requested_by, notification_target, created_at, updated_at
        FROM tasks
        WHERE task_id = ?
        """,
        (task_id,),
    )
    return None if row is None else _row_to_task(row)


def upsert_task_on_create(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    source_issue_id: str | None,
    requested_by: str,
    notification_target: str | None = None,
    created_at: datetime,
    updated_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO tasks(
            task_id, source_issue_id, state, state_rev, critical, current_branch,
            manual_gate_required, last_known_head_commit, resume_target_state,
            requested_by, notification_target, created_at, updated_at
        )
        VALUES (?, ?, ?, 0, 0, NULL, 0, NULL, NULL, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO NOTHING
        """,
        (
            task_id,
            source_issue_id,
            TaskState.NEW.value,
            requested_by,
            notification_target,
            _to_iso(created_at),
            _to_iso(updated_at),
        ),
    )


def update_task_state(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    new_state: TaskState,
    new_state_rev: int,
    updated_at: datetime,
    critical: bool | None = None,
    manual_gate_required: bool | None = None,
    resume_target_state: TaskState | None = None,
    current_branch: str | None = None,
) -> None:
    assignments = ["state = ?", "state_rev = ?", "updated_at = ?"]
    params: list[Any] = [new_state.value, new_state_rev, _to_iso(updated_at)]
    if critical is not None:
        assignments.append("critical = ?")
        params.append(_bool_to_int(critical))
    if manual_gate_required is not None:
        assignments.append("manual_gate_required = ?")
        params.append(_bool_to_int(manual_gate_required))
    if resume_target_state is not None:
        assignments.append("resume_target_state = ?")
        params.append(resume_target_state.value)
    if current_branch is not None:
        assignments.append("current_branch = ?")
        params.append(current_branch)
    params.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id = ?", tuple(params))


def update_task_notification_target(
    conn: sqlite3.Connection,
    task_id: str,
    notification_target: str | None,
) -> None:
    conn.execute(
        """
        UPDATE tasks
        SET notification_target = ?
        WHERE task_id = ?
        """,
        (notification_target, task_id),
    )


def update_last_known_head_commit(
    conn: sqlite3.Connection,
    task_id: str,
    head_commit: str,
    updated_at: datetime,
) -> None:
    conn.execute(
        """
        UPDATE tasks
        SET last_known_head_commit = ?, updated_at = ?
        WHERE task_id = ?
        """,
        (head_commit, _to_iso(updated_at), task_id),
    )


def insert_plan(conn: sqlite3.Connection, plan: Plan) -> None:
    conn.execute(
        """
        INSERT INTO plans(
            task_id, plan_rev, planner_version, plan_json, validator_score,
            validator_errors, approved_by, approved_at, approved_kind, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan.task_id,
            plan.plan_rev,
            plan.planner_version,
            _dump_json(plan.plan_json),
            plan.validator_score,
            plan.validator_errors,
            plan.approved_by,
            _maybe_iso(plan.approved_at),
            None if plan.approved_kind is None else plan.approved_kind.value,
            _to_iso(plan.created_at),
        ),
    )


def get_latest_plan(conn: sqlite3.Connection, task_id: str) -> Plan | None:
    row = fetch_one(
        conn,
        """
        SELECT task_id, plan_rev, planner_version, plan_json, validator_score,
               validator_errors, approved_by, approved_at, approved_kind, created_at
        FROM plans
        WHERE task_id = ?
        ORDER BY plan_rev DESC
        LIMIT 1
        """,
        (task_id,),
    )
    return None if row is None else _row_to_plan(row)


def approve_plan(
    conn: sqlite3.Connection,
    task_id: str,
    plan_rev: int,
    approved_by: str,
    approved_at: datetime,
    approved_kind: ApprovedKind,
) -> None:
    conn.execute(
        """
        UPDATE plans
        SET approved_by = ?, approved_at = ?, approved_kind = ?
        WHERE task_id = ? AND plan_rev = ?
        """,
        (
            approved_by,
            _to_iso(approved_at),
            approved_kind.value,
            task_id,
            plan_rev,
        ),
    )


def insert_event(conn: sqlite3.Connection, event: InboxEvent) -> bool:
    try:
        conn.execute(
            """
            INSERT INTO event_inbox(
                event_id, source, delivery_id, event_type, payload_json,
                journal_offset, received_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.source.value,
                event.delivery_id,
                event.event_type,
                _dump_json(event.payload),
                event.journal_offset,
                _to_iso(event.received_at),
            ),
        )
    except sqlite3.IntegrityError as exc:
        if "event_inbox.source, event_inbox.delivery_id" in str(exc):
            return False
        raise
    return True


def fetch_next_unprocessed(conn: sqlite3.Connection) -> InboxEvent | None:
    row = fetch_one(
        conn,
        """
        SELECT event_id, source, delivery_id, event_type, payload_json,
               journal_offset, received_at
        FROM event_inbox
        WHERE processed_at IS NULL
        ORDER BY event_id ASC
        LIMIT 1
        """,
    )
    return None if row is None else _row_to_inbox_event(row)


def mark_processed(conn: sqlite3.Connection, event_id: str, processed_at: datetime) -> None:
    conn.execute(
        """
        UPDATE event_inbox
        SET processed_at = ?
        WHERE event_id = ?
        """,
        (_to_iso(processed_at), event_id),
    )


def insert_outbox(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    stream: Stream,
    target: str,
    origin_event_id: str,
    payload_json: str,
    state_rev: int,
    idempotency_key: str,
    next_attempt_at: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO projection_outbox(
            task_id, stream, target, origin_event_id, payload_json,
            state_rev, idempotency_key, next_attempt_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stream, target, idempotency_key) DO NOTHING
        """,
        (
            task_id,
            stream.value,
            target,
            origin_event_id,
            payload_json,
            state_rev,
            idempotency_key,
            next_attempt_at,
        ),
    )
    if cursor.rowcount == 1:
        return int(cursor.lastrowid)
    row = fetch_one(
        conn,
        """
        SELECT outbox_id
        FROM projection_outbox
        WHERE stream = ? AND target = ? AND idempotency_key = ?
        """,
        (stream.value, target, idempotency_key),
    )
    if row is None:
        raise sqlite3.IntegrityError("projection_outbox unique conflict lookup failed")
    return int(row["outbox_id"])


def claim_next_outbox(
    conn: sqlite3.Connection,
    worker_id: str,
    now_iso: str,
) -> OutboxRecord | None:
    with tx(conn):
        row = fetch_one(
            conn,
            """
            SELECT outbox_id
            FROM projection_outbox AS candidate
            WHERE candidate.sent_at IS NULL
              AND candidate.next_attempt_at <= ?
              AND candidate.claimed_by IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM projection_outbox AS inflight
                  WHERE inflight.task_id = candidate.task_id
                    AND inflight.stream = candidate.stream
                    AND inflight.target = candidate.target
                    AND inflight.sent_at IS NULL
                    AND inflight.claimed_by IS NOT NULL
              )
            ORDER BY candidate.outbox_id ASC
            LIMIT 1
            """,
            (now_iso,),
        )
        if row is None:
            return None
        outbox_id = int(row["outbox_id"])
        conn.execute(
            """
            UPDATE projection_outbox
            SET claimed_by = ?, claimed_at = ?
            WHERE outbox_id = ? AND claimed_by IS NULL AND sent_at IS NULL
            """,
            (worker_id, now_iso, outbox_id),
        )
        claimed = fetch_one(
            conn,
            """
            SELECT outbox_id, task_id, stream, target, origin_event_id, payload_json,
                   state_rev, idempotency_key, attempt_count, next_attempt_at, sent_at
            FROM projection_outbox
            WHERE outbox_id = ?
            """,
            (outbox_id,),
        )
    return None if claimed is None else _row_to_outbox_record(claimed)


def mark_outbox_sent(conn: sqlite3.Connection, outbox_id: int, sent_at: datetime) -> None:
    conn.execute(
        """
        UPDATE projection_outbox
        SET sent_at = ?
        WHERE outbox_id = ?
        """,
        (_to_iso(sent_at), outbox_id),
    )


def reschedule_outbox(
    conn: sqlite3.Connection,
    outbox_id: int,
    next_attempt_at: datetime,
    attempt_count: int,
) -> None:
    conn.execute(
        """
        UPDATE projection_outbox
        SET next_attempt_at = ?, attempt_count = ?, claimed_by = NULL, claimed_at = NULL
        WHERE outbox_id = ?
        """,
        (_to_iso(next_attempt_at), attempt_count, outbox_id),
    )


def upsert_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    stream: Stream,
    target: str,
    last_sent_state_rev: int,
    last_sent_outbox_id: int,
    updated_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO projection_cursors(
            task_id, stream, target, last_sent_state_rev, last_sent_outbox_id, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id, stream, target)
        DO UPDATE SET
            last_sent_state_rev = excluded.last_sent_state_rev,
            last_sent_outbox_id = excluded.last_sent_outbox_id,
            updated_at = excluded.updated_at
        """,
        (
            task_id,
            stream.value,
            target,
            last_sent_state_rev,
            last_sent_outbox_id,
            _to_iso(updated_at),
        ),
    )


def get_cursor(
    conn: sqlite3.Connection,
    task_id: str,
    stream: Stream,
    target: str,
) -> tuple[int, int] | None:
    row = fetch_one(
        conn,
        """
        SELECT last_sent_state_rev, last_sent_outbox_id
        FROM projection_cursors
        WHERE task_id = ? AND stream = ? AND target = ?
        """,
        (task_id, stream.value, target),
    )
    if row is None:
        return None
    return int(row["last_sent_state_rev"]), int(row["last_sent_outbox_id"])


def enqueue_waiter(
    conn: sqlite3.Connection,
    branch: str,
    task_id: str,
    status: BranchWaiterStatus = BranchWaiterStatus.QUEUED,
) -> int:
    row = fetch_one(
        conn,
        """
        SELECT COALESCE(MAX(queue_order), 0) + 1 AS next_queue_order
        FROM branch_waiters
        WHERE branch = ?
        """,
        (branch,),
    )
    queue_order = int(row["next_queue_order"]) if row is not None else 1
    conn.execute(
        """
        INSERT INTO branch_waiters(branch, task_id, queue_order, status)
        VALUES (?, ?, ?, ?)
        """,
        (branch, task_id, queue_order, status.value),
    )
    return queue_order


def peek_head_waiter(conn: sqlite3.Connection, branch: str) -> tuple[str, int] | None:
    row = fetch_one(
        conn,
        """
        SELECT task_id, queue_order
        FROM branch_waiters
        WHERE branch = ? AND status = ?
        ORDER BY queue_order ASC
        LIMIT 1
        """,
        (branch, BranchWaiterStatus.QUEUED.value),
    )
    if row is None:
        return None
    return str(row["task_id"]), int(row["queue_order"])


def update_waiter_status(
    conn: sqlite3.Connection,
    branch: str,
    task_id: str,
    status: BranchWaiterStatus,
) -> None:
    conn.execute(
        """
        UPDATE branch_waiters
        SET status = ?
        WHERE branch = ? AND task_id = ?
        """,
        (status.value, branch, task_id),
    )


def remove_waiter(conn: sqlite3.Connection, branch: str, task_id: str) -> None:
    conn.execute(
        """
        DELETE FROM branch_waiters
        WHERE branch = ? AND task_id = ?
        """,
        (branch, task_id),
    )


def next_branch_token(conn: sqlite3.Connection, branch: str) -> int:
    with tx(conn):
        conn.execute(
            """
            INSERT INTO branch_tokens(branch, last_token)
            VALUES (?, 0)
            ON CONFLICT(branch) DO NOTHING
            """,
            (branch,),
        )
        conn.execute(
            """
            UPDATE branch_tokens
            SET last_token = last_token + 1
            WHERE branch = ?
            """,
            (branch,),
        )
        row = fetch_one(
            conn,
            """
            SELECT last_token
            FROM branch_tokens
            WHERE branch = ?
            """,
            (branch,),
        )
    if row is None:
        raise sqlite3.IntegrityError("branch_tokens update failed")
    return int(row["last_token"])


def get_ingester_state(conn: sqlite3.Connection) -> tuple[str | None, int]:
    row = fetch_one(
        conn,
        """
        SELECT last_file, last_offset
        FROM journal_ingester_state
        WHERE singleton_id = 1
        """,
    )
    if row is None:
        return None, 0
    return row["last_file"], int(row["last_offset"])


def update_ingester_state(
    conn: sqlite3.Connection,
    last_file: str | None,
    last_offset: int,
    updated_at: datetime,
) -> None:
    conn.execute(
        """
        UPDATE journal_ingester_state
        SET last_file = ?, last_offset = ?, updated_at = ?
        WHERE singleton_id = 1
        """,
        (last_file, last_offset, _to_iso(updated_at)),
    )


def insert_tool_call(conn: sqlite3.Connection, rec: ToolCallRecord) -> None:
    conn.execute(
        """
        INSERT INTO tool_calls(
            call_id, task_id, stage, tool_name, started_at, ended_at, duration_ms,
            success, exit_code, failure_code, log_path, log_sha256, log_bytes,
            tokens_in, tokens_out
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rec.call_id,
            rec.task_id,
            rec.stage.value,
            rec.tool_name,
            _to_iso(rec.started_at),
            _maybe_iso(rec.ended_at),
            rec.duration_ms,
            _maybe_bool_to_int(rec.success),
            rec.exit_code,
            rec.failure_code,
            rec.log_path,
            rec.log_sha256,
            rec.log_bytes,
            rec.tokens_in,
            rec.tokens_out,
        ),
    )


def update_tool_call_end(
    conn: sqlite3.Connection,
    call_id: str,
    ended_at: datetime,
    duration_ms: int,
    success: bool,
    exit_code: int | None,
    failure_code: str | None,
    log_path: str | None,
    log_sha256: str | None,
    log_bytes: int | None,
    tokens_in: int | None,
    tokens_out: int | None,
) -> None:
    conn.execute(
        """
        UPDATE tool_calls
        SET ended_at = ?, duration_ms = ?, success = ?, exit_code = ?, failure_code = ?,
            log_path = ?, log_sha256 = ?, log_bytes = ?, tokens_in = ?, tokens_out = ?
        WHERE call_id = ?
        """,
        (
            _to_iso(ended_at),
            duration_ms,
            _bool_to_int(success),
            exit_code,
            failure_code,
            log_path,
            log_sha256,
            log_bytes,
            tokens_in,
            tokens_out,
            call_id,
        ),
    )


def null_log_metadata(conn: sqlite3.Connection, call_id: str) -> None:
    conn.execute(
        """
        UPDATE tool_calls
        SET log_path = NULL, log_sha256 = NULL, log_bytes = NULL
        WHERE call_id = ?
        """,
        (call_id,),
    )


def upsert_rate_window(conn: sqlite3.Connection, rw: RateWindow) -> None:
    conn.execute(
        """
        INSERT INTO rate_windows(
            tool_name, window_started_at, window_reset_at, remaining, "limit", updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(tool_name)
        DO UPDATE SET
            window_started_at = excluded.window_started_at,
            window_reset_at = excluded.window_reset_at,
            remaining = excluded.remaining,
            "limit" = excluded."limit",
            updated_at = excluded.updated_at
        """,
        (
            rw.tool_name,
            _to_iso(rw.window_started_at),
            _to_iso(rw.window_reset_at),
            rw.remaining,
            rw.limit,
            _to_iso(rw.updated_at),
        ),
    )


def get_rate_window(conn: sqlite3.Connection, tool_name: str) -> RateWindow | None:
    row = fetch_one(
        conn,
        """
        SELECT tool_name, window_started_at, window_reset_at, remaining, "limit", updated_at
        FROM rate_windows
        WHERE tool_name = ?
        """,
        (tool_name,),
    )
    return None if row is None else _row_to_rate_window(row)


def insert_system_event(
    conn: sqlite3.Connection,
    *,
    task_id: str | None,
    event_type: str,
    severity: Severity,
    payload_json: str,
    created_at: datetime,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO system_events(task_id, event_type, severity, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, event_type, severity.value, payload_json, _to_iso(created_at)),
    )
    return int(cursor.lastrowid)


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=str(row["task_id"]),
        source_issue_id=row["source_issue_id"],
        state=TaskState(row["state"]),
        state_rev=int(row["state_rev"]),
        critical=bool(row["critical"]),
        current_branch=row["current_branch"],
        manual_gate_required=bool(row["manual_gate_required"]),
        last_known_head_commit=row["last_known_head_commit"],
        resume_target_state=None
        if row["resume_target_state"] is None
        else TaskState(row["resume_target_state"]),
        requested_by=str(row["requested_by"]),
        notification_target=None
        if row["notification_target"] is None
        else str(row["notification_target"]),
        created_at=_parse_datetime(str(row["created_at"])),
        updated_at=_parse_datetime(str(row["updated_at"])),
    )


def _row_to_plan(row: sqlite3.Row) -> Plan:
    return Plan(
        task_id=str(row["task_id"]),
        plan_rev=int(row["plan_rev"]),
        planner_version=str(row["planner_version"]),
        plan_json=json.loads(str(row["plan_json"])),
        validator_score=int(row["validator_score"]),
        validator_errors=int(row["validator_errors"]),
        approved_by=row["approved_by"],
        approved_at=None if row["approved_at"] is None else _parse_datetime(str(row["approved_at"])),
        approved_kind=None if row["approved_kind"] is None else ApprovedKind(row["approved_kind"]),
        created_at=_parse_datetime(str(row["created_at"])),
    )


def _row_to_inbox_event(row: sqlite3.Row) -> InboxEvent:
    return InboxEvent(
        event_id=str(row["event_id"]),
        source=Source(row["source"]),
        delivery_id=str(row["delivery_id"]),
        event_type=str(row["event_type"]),
        payload=json.loads(str(row["payload_json"])),
        journal_offset=int(row["journal_offset"]),
        received_at=_parse_datetime(str(row["received_at"])),
    )


def _row_to_outbox_record(row: sqlite3.Row) -> OutboxRecord:
    return OutboxRecord(
        outbox_id=int(row["outbox_id"]),
        task_id=str(row["task_id"]),
        stream=Stream(row["stream"]),
        target=str(row["target"]),
        origin_event_id=str(row["origin_event_id"]),
        payload=json.loads(str(row["payload_json"])),
        state_rev=int(row["state_rev"]),
        idempotency_key=str(row["idempotency_key"]),
        attempt_count=int(row["attempt_count"]),
        next_attempt_at=_parse_datetime(str(row["next_attempt_at"])),
        sent_at=None if row["sent_at"] is None else _parse_datetime(str(row["sent_at"])),
    )


def _row_to_rate_window(row: sqlite3.Row) -> RateWindow:
    return RateWindow(
        tool_name=str(row["tool_name"]),
        window_started_at=_parse_datetime(str(row["window_started_at"])),
        window_reset_at=_parse_datetime(str(row["window_reset_at"])),
        remaining=int(row["remaining"]),
        limit=int(row["limit"]),
        updated_at=_parse_datetime(str(row["updated_at"])),
    )


def _dump_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _parse_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _maybe_iso(value: datetime | None) -> str | None:
    return None if value is None else _to_iso(value)


def _bool_to_int(value: bool) -> int:
    return 1 if value else 0


def _maybe_bool_to_int(value: bool | None) -> int | None:
    return None if value is None else _bool_to_int(value)
