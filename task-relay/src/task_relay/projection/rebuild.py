from __future__ import annotations

import json
import sqlite3

from task_relay.clock import Clock, SystemClock
from task_relay.config import Settings
from task_relay.db.connection import tx
from task_relay.db.queries import get_latest_plan, get_task
from task_relay.projection.labels import MANAGED_LABELS
from task_relay.router.idempotency import label_sync_key, snapshot_key
from task_relay.types import Stream, Task, TaskState


def _task_url(task: Task) -> str:
    base_url = Settings().forgejo_base_url
    if task.source_issue_id:
        return f"{base_url}/issues/{task.source_issue_id}"
    return f"{base_url}/tasks/{task.task_id}"


def _desired_labels(state: TaskState, *, critical: bool) -> list[str]:
    labels: set[str] = set()
    if critical:
        labels.add("critical")
    if state is TaskState.HUMAN_REVIEW_REQUIRED:
        labels.add("human_review_required")
    if state is TaskState.CANCELLED:
        labels.add("cancelled")
    return sorted(labels)


def _insert_outbox_row(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    stream: Stream,
    target: str,
    origin_event_id: str,
    payload: dict[str, object],
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
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            state_rev,
            idempotency_key,
            next_attempt_at,
        ),
    )
    return int(cursor.rowcount)


def rebuild_for_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    force: bool = False,
    clock: Clock = SystemClock(),
) -> int:
    task = get_task(conn, task_id)
    if task is None:
        raise ValueError(f"task not found: {task_id}")
    latest_plan = get_latest_plan(conn, task_id)
    target = task.source_issue_id or task.task_id
    now_iso = clock.now().isoformat()
    origin_event_id = f"projection-rebuild:{task_id}:{now_iso}"
    snapshot_payload = {
        "state": task.state.value,
        "state_rev": task.state_rev,
        "plan_rev": latest_plan.plan_rev if latest_plan is not None else None,
        "critical": task.critical,
        "task_url": _task_url(task),
    }
    desired_labels = _desired_labels(task.state, critical=task.critical)
    label_payload = {
        "desired_labels": desired_labels,
        "managed_labels": sorted(MANAGED_LABELS),
    }

    with tx(conn):
        if force:
            conn.execute(
                """
                DELETE FROM projection_outbox
                WHERE task_id = ? AND sent_at IS NULL
                """,
                (task_id,),
            )
        inserted = _insert_outbox_row(
            conn,
            task_id=task.task_id,
            stream=Stream.TASK_SNAPSHOT,
            target=target,
            origin_event_id=origin_event_id,
            payload=snapshot_payload,
            state_rev=task.state_rev,
            idempotency_key=snapshot_key(task.task_id, target, task.state_rev, snapshot_payload),
            next_attempt_at=now_iso,
        )
        inserted += _insert_outbox_row(
            conn,
            task_id=task.task_id,
            stream=Stream.TASK_LABEL_SYNC,
            target=target,
            origin_event_id=origin_event_id,
            payload=label_payload,
            state_rev=task.state_rev,
            idempotency_key=label_sync_key(task.task_id, target, task.state_rev, desired_labels),
            next_attempt_at=now_iso,
        )
    return inserted
