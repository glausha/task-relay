from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from task_relay.clock import Clock, SystemClock
from task_relay.types import RateWindow


SUBSCRIPTION_WINDOW_SECONDS = 5 * 3600
STOP_NEW_TASKS_RATIO = 0.2


def should_stop_new_tasks(remaining: int, limit: int) -> bool:
    return limit > 0 and remaining < limit * STOP_NEW_TASKS_RATIO


class RateTracker:
    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        clock: Clock = SystemClock(),
    ) -> None:
        self._conn_factory = conn_factory
        self._clock = clock

    def observe_api_headers(
        self,
        tool_name: str,
        remaining: int,
        limit: int,
        reset_at: datetime,
    ) -> None:
        now = self._clock.now()
        current = self.snapshot(tool_name)
        window_started_at = (
            current.window_started_at
            if current is not None and current.window_reset_at == reset_at
            else now
        )
        self._upsert(
            tool_name=tool_name,
            window_started_at=window_started_at,
            window_reset_at=reset_at,
            remaining=remaining,
            limit=limit,
            updated_at=now,
        )

    def observe_subscription_use(self, tool_name: str) -> None:
        now = self._clock.now()
        current = self.snapshot(tool_name)
        if current is None or now >= current.window_reset_at:
            limit = current.limit if current is not None else 0
            self._upsert(
                tool_name=tool_name,
                window_started_at=now,
                window_reset_at=now + timedelta(seconds=SUBSCRIPTION_WINDOW_SECONDS),
                remaining=limit,
                limit=limit,
                updated_at=now,
            )
            return
        self._upsert(
            tool_name=tool_name,
            window_started_at=current.window_started_at,
            window_reset_at=current.window_reset_at,
            remaining=current.remaining,
            limit=current.limit,
            updated_at=now,
        )

    def observe_subscription_429(self, tool_name: str) -> None:
        now = self._clock.now()
        current = self.snapshot(tool_name)
        if current is None or now >= current.window_reset_at:
            limit = current.limit if current is not None else 0
            self._upsert(
                tool_name=tool_name,
                window_started_at=now,
                window_reset_at=now + timedelta(seconds=SUBSCRIPTION_WINDOW_SECONDS),
                remaining=0,
                limit=limit,
                updated_at=now,
            )
            return
        self._upsert(
            tool_name=tool_name,
            window_started_at=current.window_started_at,
            window_reset_at=current.window_reset_at,
            remaining=0,
            limit=current.limit,
            updated_at=now,
        )

    def snapshot(self, tool_name: str) -> RateWindow | None:
        conn = self._conn_factory()
        row = conn.execute(
            """
            SELECT tool_name, window_started_at, window_reset_at, remaining, "limit", updated_at
            FROM rate_windows
            WHERE tool_name = ?
            """,
            (tool_name,),
        ).fetchone()
        if row is None:
            return None
        return RateWindow(
            tool_name=row[0],
            window_started_at=_parse_datetime(row[1]),
            window_reset_at=_parse_datetime(row[2]),
            remaining=row[3],
            limit=row[4],
            updated_at=_parse_datetime(row[5]),
        )

    def stop_new_tasks_any(self) -> bool:
        conn = self._conn_factory()
        rows = conn.execute('SELECT remaining, "limit" FROM rate_windows').fetchall()
        return any(should_stop_new_tasks(remaining=row[0], limit=row[1]) for row in rows)

    def _upsert(
        self,
        *,
        tool_name: str,
        window_started_at: datetime,
        window_reset_at: datetime,
        remaining: int,
        limit: int,
        updated_at: datetime,
    ) -> None:
        conn = self._conn_factory()
        conn.execute(
            """
            INSERT INTO rate_windows(tool_name, window_started_at, window_reset_at, remaining, "limit", updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tool_name) DO UPDATE SET
                window_started_at = excluded.window_started_at,
                window_reset_at = excluded.window_reset_at,
                remaining = excluded.remaining,
                "limit" = excluded."limit",
                updated_at = excluded.updated_at
            """,
            (
                tool_name,
                window_started_at.isoformat(),
                window_reset_at.isoformat(),
                remaining,
                limit,
                updated_at.isoformat(),
            ),
        )


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
