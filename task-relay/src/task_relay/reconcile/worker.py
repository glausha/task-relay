"""Reconcile worker: detailed-design §11."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..clock import Clock, SystemClock
from ..db import queries
from ..db.connection import fetch_all, fetch_one
from ..ids import new_event_id
from ..journal.writer import JournalWriter
from ..types import CanonicalEvent, Severity, Source, Stage, TaskState


class ReconcileWorker:
    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        journal_writer: JournalWriter,
        *,
        implementing_grace_seconds: int = 120,
        heartbeat_seconds: int = 60,
        degrade_hours: int = 24,
        clock: Clock = SystemClock(),
    ) -> None:
        self._conn_factory = conn_factory
        self._journal_writer = journal_writer
        self._implementing_grace_seconds = implementing_grace_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._degrade_hours = degrade_hours
        self._clock = clock

    def run_once(self) -> dict[str, int]:
        """1 回 sweep。返り値 {'implementing_checked': n, 'events_emitted': n, 'degraded_aged': n}"""
        conn = self._conn_factory()
        now = self._clock.now()
        implementing_rows = fetch_all(
            conn,
            """
            SELECT task_id, state, lease_branch, feature_branch, worktree_path,
                   last_known_head_commit, updated_at
            FROM tasks
            WHERE state = ?
            ORDER BY task_id ASC
            """,
            (TaskState.IMPLEMENTING.value,),
        )
        implementing_checked = 0
        events_emitted = 0
        try:
            for row in implementing_rows:
                implementing_checked += 1
                task_id = str(row["task_id"])
                last_known_head_commit = row["last_known_head_commit"]
                latest_plan = queries.get_latest_plan(conn, task_id)
                plan_rev = None if latest_plan is None else latest_plan.plan_rev
                latest_tool_call = self._get_latest_executing_tool_call(conn, task_id)
                heartbeat_fresh = self._is_heartbeat_fresh(latest_tool_call=latest_tool_call, now=now)
                worktree_clean = self._is_worktree_clean(
                    last_known_head_commit=last_known_head_commit,
                    latest_tool_call=latest_tool_call,
                )
                event_id = new_event_id()
                self._journal_writer.append(
                    CanonicalEvent(
                        event_id=event_id,
                        source=Source.INTERNAL,
                        delivery_id=event_id,
                        event_type="internal.reconcile_resume",
                        payload={
                            "task_id": task_id,
                            "plan_rev": plan_rev,
                            "lease_branch": row["lease_branch"],
                            "feature_branch": row["feature_branch"],
                            "worktree_path": row["worktree_path"],
                            "worktree_clean": worktree_clean,
                            "heartbeat_fresh": heartbeat_fresh,
                            "last_known_head_commit": last_known_head_commit,
                            "observed_at": _to_iso(now),
                        },
                        received_at=now,
                        request_id=None,
                    )
                )
                events_emitted += 1
            degraded_aged = self._insert_aged_degraded_events(conn, now)
            return {
                "implementing_checked": implementing_checked,
                "events_emitted": events_emitted,
                "degraded_aged": degraded_aged,
            }
        finally:
            conn.close()

    def _get_latest_executing_tool_call(
        self,
        conn: sqlite3.Connection,
        task_id: str,
    ) -> sqlite3.Row | None:
        return fetch_one(
            conn,
            """
            SELECT ended_at, success
            FROM tool_calls
            WHERE task_id = ? AND stage = ? AND ended_at IS NOT NULL
            ORDER BY ended_at DESC, call_id DESC
            LIMIT 1
            """,
            (task_id, Stage.EXECUTING.value),
        )

    def _is_heartbeat_fresh(self, *, latest_tool_call: sqlite3.Row | None, now: datetime) -> bool:
        if latest_tool_call is None or latest_tool_call["ended_at"] is None:
            return False
        ended_at = _parse_datetime(str(latest_tool_call["ended_at"]))
        return ended_at >= now - timedelta(seconds=self._heartbeat_seconds)

    def _is_worktree_clean(
        self,
        *,
        last_known_head_commit: str | None,
        latest_tool_call: sqlite3.Row | None,
    ) -> bool:
        if last_known_head_commit is None:
            return True
        if latest_tool_call is None:
            return False
        return bool(latest_tool_call["success"])

    def _insert_aged_degraded_events(self, conn: sqlite3.Connection, now: datetime) -> int:
        threshold = now - timedelta(hours=self._degrade_hours)
        rows = fetch_all(
            conn,
            """
            SELECT task_id, updated_at
            FROM tasks
            WHERE state = ? AND updated_at < ?
            ORDER BY task_id ASC
            """,
            (TaskState.SYSTEM_DEGRADED.value, _to_iso(threshold)),
        )
        for row in rows:
            updated_at = _parse_datetime(str(row["updated_at"]))
            aged_hours = int((now - updated_at).total_seconds() // 3600)
            conn.execute(
                """
                INSERT INTO system_events(task_id, event_type, severity, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(row["task_id"]),
                    "reconcile_degraded_aged",
                    Severity.WARNING.value,
                    json.dumps(
                        {"task_id": str(row["task_id"]), "aged_hours": aged_hours},
                        separators=(",", ":"),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    _to_iso(now),
                ),
            )
        return len(rows)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
