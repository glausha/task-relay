from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from task_relay.breaker.circuit_breaker import CircuitBreaker
from task_relay.config import Settings
from task_relay.rate.windows import should_stop_new_tasks
from task_relay.types import TaskState


@dataclass(frozen=True)
class RateWindowSnapshot:
    tool_name: str
    remaining: int
    limit: int
    window_reset_at: str


@dataclass(frozen=True)
class StatusSnapshot:
    state_counts: dict[str, int]
    in_progress: int
    waiting_human: int
    global_degraded: int
    breaker_state: str
    breaker_open_codes: list[str]
    rate_protected: bool
    scope_label: str
    rate_windows: list[RateWindowSnapshot]

    def render_lines(self) -> list[str]:
        lines = [f"{state.value}={self.state_counts[state.value]}" for state in TaskState]
        lines.append(f"in_progress={self.in_progress}")
        lines.append(f"waiting_human={self.waiting_human}")
        lines.append(f"global_degraded={self.global_degraded}")
        lines.append(f"scope_label={self.scope_label}")
        lines.append(f"breaker_state={self.breaker_state}")
        lines.append(f"breaker_open_codes={self.breaker_open_codes}")
        lines.append(f"rate_stop_new_tasks={'on' if self.rate_protected else 'off'}")
        for row in self.rate_windows:
            lines.append(f"rate[{row.tool_name}]={row.remaining}/{row.limit} reset_at={row.window_reset_at}")
        return lines


def load_status_snapshot(conn: sqlite3.Connection, settings: Settings) -> StatusSnapshot:
    state_rows = conn.execute(
        """
        SELECT state, COUNT(*) AS count
        FROM tasks
        GROUP BY state
        """
    ).fetchall()
    rate_rows = conn.execute(
        """
        SELECT tool_name, remaining, "limit", window_reset_at
        FROM rate_windows
        ORDER BY tool_name ASC
        """
    ).fetchall()
    counts = {str(row["state"]): int(row["count"]) for row in state_rows}
    state_counts = {state.value: counts.get(state.value, 0) for state in TaskState}
    in_progress = sum(
        state_counts[state.value]
        for state in (
            TaskState.PLANNING,
            TaskState.PLAN_APPROVED,
            TaskState.IMPLEMENTING,
            TaskState.REVIEWING,
        )
    )
    waiting_human = sum(
        state_counts[state.value]
        for state in (
            TaskState.PLAN_PENDING_APPROVAL,
            TaskState.HUMAN_REVIEW_REQUIRED,
            TaskState.NEEDS_FIX,
        )
    )
    global_degraded = state_counts[TaskState.SYSTEM_DEGRADED.value]
    rate_windows = [
        RateWindowSnapshot(
            tool_name=str(row["tool_name"]),
            remaining=int(row["remaining"]),
            limit=int(row["limit"]),
            window_reset_at=str(row["window_reset_at"]),
        )
        for row in rate_rows
    ]
    rate_protected = any(
        should_stop_new_tasks(remaining=row.remaining, limit=row.limit) for row in rate_windows
    )
    breaker_open_codes = _load_breaker_open_codes(conn, settings)
    return StatusSnapshot(
        state_counts=state_counts,
        in_progress=in_progress,
        waiting_human=waiting_human,
        global_degraded=global_degraded,
        breaker_state="open" if breaker_open_codes else "closed",
        breaker_open_codes=breaker_open_codes,
        rate_protected=rate_protected,
        scope_label=scope_label(
            in_progress=in_progress,
            waiting_human=waiting_human,
            global_degraded=global_degraded,
            breaker_open=bool(breaker_open_codes),
            rate_protected=rate_protected,
        ),
        rate_windows=rate_windows,
    )


def empty_status_snapshot() -> StatusSnapshot:
    state_counts = {state.value: 0 for state in TaskState}
    return StatusSnapshot(
        state_counts=state_counts,
        in_progress=0,
        waiting_human=0,
        global_degraded=0,
        breaker_state="closed",
        breaker_open_codes=[],
        rate_protected=False,
        scope_label=scope_label(
            in_progress=0,
            waiting_human=0,
            global_degraded=0,
            breaker_open=False,
            rate_protected=False,
        ),
        rate_windows=[],
    )


def scope_label(
    *,
    in_progress: int,
    waiting_human: int,
    global_degraded: int,
    breaker_open: bool,
    rate_protected: bool,
) -> str:
    if global_degraded > 0:
        return "全体障害"
    if breaker_open or rate_protected:
        return "全体保護中"
    if waiting_human > 0:
        return "局所障害"
    if in_progress > 0:
        return "進行中"
    return "待機中"


def _load_breaker_open_codes(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'system_events'").fetchone() is None:
        return []
    breaker = CircuitBreaker(
        window_seconds=settings.breaker_window_seconds,
        fatal_threshold=settings.breaker_fatal_threshold,
    )
    # WHY: status must rebuild the durable breaker window after process restarts.
    breaker.rebuild_from_events(conn, window_seconds=settings.breaker_window_seconds)
    return sorted(code.value for code in breaker.open_codes())
