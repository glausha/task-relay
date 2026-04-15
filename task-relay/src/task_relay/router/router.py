from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from importlib import import_module

from task_relay.clock import Clock, SystemClock
from task_relay.config import Settings
from task_relay.ids import new_task_id_from_event
from task_relay.router.guards import GuardContext
from task_relay.router.state_machine import TRANSITIONS, TransitionKey
from task_relay.types import InboxEvent, Task, TaskState


@dataclass(frozen=True)
class RouterResult:
    event_id: str
    task_id: str | None
    from_state: TaskState | None
    to_state: TaskState | None
    outbox_ids: list[int]
    skipped: bool
    skip_reason: str | None


class Router:
    def __init__(self, settings: Settings, clock: Clock = SystemClock()) -> None:
        self._settings = settings
        self._clock = clock

    def run_once(self, conn: sqlite3.Connection, event: InboxEvent) -> RouterResult:
        queries = import_module("task_relay.db.queries")
        conn.execute("BEGIN IMMEDIATE")
        try:
            task = None
            task_id = event.payload.get("task_id")
            if task_id is not None:
                task = queries.get_task(conn, task_id)
            if task is None and event.event_type == "issues.opened":
                task = self._create_new_task(conn, event)
            if task is None:
                queries.mark_processed(conn, event.event_id, event.received_at)
                conn.execute("COMMIT")
                return RouterResult(
                    event_id=event.event_id,
                    task_id=None,
                    from_state=None,
                    to_state=None,
                    outbox_ids=[],
                    skipped=True,
                    skip_reason="task_not_found",
                )
            latest_plan = queries.get_latest_plan(conn, task.task_id)
            specs = TRANSITIONS.get(TransitionKey(state=task.state, event_type=event.event_type))
            if not specs:
                queries.mark_processed(conn, event.event_id, event.received_at)
                conn.execute("COMMIT")
                return RouterResult(
                    event_id=event.event_id,
                    task_id=task.task_id,
                    from_state=task.state,
                    to_state=None,
                    outbox_ids=[],
                    skipped=True,
                    skip_reason="no_transition",
                )
            ctx = GuardContext(
                task=task,
                event=event,
                latest_plan=latest_plan,
                critical=task.critical,
                settings=self._settings,
                clock=self._clock,
                conn=conn,
            )
            selected = None
            to_state = None
            for spec in specs:
                if spec.guard(ctx):
                    selected = spec
                    to_state = spec.to_state_fn(ctx)
                    break
            if selected is None or to_state is None:
                queries.mark_processed(conn, event.event_id, event.received_at)
                conn.execute("COMMIT")
                return RouterResult(
                    event_id=event.event_id,
                    task_id=task.task_id,
                    from_state=task.state,
                    to_state=None,
                    outbox_ids=[],
                    skipped=True,
                    skip_reason="no_guard_matched",
                )
            if selected.on_apply is not None:
                selected.on_apply(ctx)
            queries.mark_processed(conn, event.event_id, event.received_at)
            outbox_ids = self._outbox_ids_for_event(conn, event.event_id)
            conn.execute("COMMIT")
            return RouterResult(
                event_id=event.event_id,
                task_id=task.task_id,
                from_state=task.state,
                to_state=to_state,
                outbox_ids=outbox_ids,
                skipped=False,
                skip_reason=None,
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _create_new_task(self, conn: sqlite3.Connection, event: InboxEvent) -> Task:
        queries = import_module("task_relay.db.queries")
        source_issue_id = event.payload.get("source_issue_id") or event.payload.get("issue_id")
        requested_by = str(event.payload.get("requested_by") or event.payload.get("actor") or "unknown")
        task_id = new_task_id_from_event(event.event_id)
        queries.upsert_task_on_create(
            conn,
            task_id=task_id,
            source_issue_id=None if source_issue_id is None else str(source_issue_id),
            requested_by=requested_by,
            created_at=event.received_at,
            updated_at=event.received_at,
        )
        task = queries.get_task(conn, task_id)
        if task is None:
            raise RuntimeError(f"task creation failed for {task_id}")
        return task

    def _outbox_ids_for_event(self, conn: sqlite3.Connection, event_id: str) -> list[int]:
        rows = conn.execute(
            "SELECT outbox_id FROM projection_outbox WHERE origin_event_id = ? ORDER BY outbox_id",
            (event_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]


def run_once(conn: sqlite3.Connection, event: InboxEvent) -> RouterResult:
    return Router(Settings()).run_once(conn, event)
