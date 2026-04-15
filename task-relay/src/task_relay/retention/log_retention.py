from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from task_relay.clock import Clock, SystemClock
from task_relay.db.connection import tx


FULL_LOG_RETAIN_DAYS = 30
DIGEST_RETAIN_DAYS = 180


class LogRetention:
    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        log_base_dir: Path,
        clock: Clock = SystemClock(),
    ) -> None:
        self._conn_factory = conn_factory
        self._log_base_dir = log_base_dir
        self._clock = clock

    def sweep(self) -> dict[str, int]:
        now = self._clock.now()
        full_cutoff = now - timedelta(days=FULL_LOG_RETAIN_DAYS)
        digest_cutoff = now - timedelta(days=DIGEST_RETAIN_DAYS)
        counts = {
            "nulled": 0,
            "deleted_files": 0,
            "deleted_metadata": 0,
            "orphan_files": 0,
            "stale_metadata": 0,
        }
        conn = self._conn_factory()
        rows = conn.execute(
            "SELECT call_id, task_id, started_at, log_path FROM tool_calls"
        ).fetchall()

        to_null: list[tuple[str, Path]] = []
        to_delete_metadata: list[str] = []
        for call_id, task_id, started_at_raw, log_path in rows:
            started_at = _parse_datetime(started_at_raw)
            if started_at < digest_cutoff:
                to_delete_metadata.append(call_id)
                continue
            if isinstance(log_path, str):
                resolved_path = self._resolve_db_path(log_path)
                if started_at < full_cutoff:
                    to_null.append((call_id, resolved_path))

        if to_null:
            nulled = 0
            with tx(conn):
                for call_id, _ in to_null:
                    result = conn.execute(
                        """
                        UPDATE tool_calls
                        SET log_path = NULL, log_sha256 = NULL, log_bytes = NULL
                        WHERE call_id = ? AND log_path IS NOT NULL
                        """,
                        (call_id,),
                    )
                    nulled += result.rowcount
            counts["nulled"] = nulled

        for _, path in to_null:
            if path.exists():
                path.unlink()
                counts["deleted_files"] += 1

        if to_delete_metadata:
            deleted_metadata = 0
            with tx(conn):
                for call_id in to_delete_metadata:
                    result = conn.execute("DELETE FROM tool_calls WHERE call_id = ?", (call_id,))
                    deleted_metadata += result.rowcount
            counts["deleted_metadata"] = deleted_metadata

        referenced_existing = {
            str(self._resolve_db_path(log_path))
            for (log_path,) in conn.execute(
                "SELECT log_path FROM tool_calls WHERE log_path IS NOT NULL"
            ).fetchall()
            if isinstance(log_path, str)
        }

        for path in self._iter_log_files():
            if str(path) in referenced_existing:
                continue
            path.unlink()
            counts["orphan_files"] += 1
            with tx(conn):
                self._append_orphan_event(
                    conn,
                    task_id=None,
                    payload={
                        "orphan_kind": "file",
                        "path": str(path),
                        "detected_by": "retention_sweep",
                    },
                    created_at=now,
                )

        stale_rows = conn.execute(
            "SELECT call_id, task_id, log_path FROM tool_calls WHERE log_path IS NOT NULL"
        ).fetchall()
        for call_id, task_id, log_path in stale_rows:
            path = self._resolve_db_path(log_path)
            if path.exists():
                continue
            with tx(conn):
                conn.execute(
                    """
                    UPDATE tool_calls
                    SET log_path = NULL, log_sha256 = NULL, log_bytes = NULL
                    WHERE call_id = ?
                    """,
                    (call_id,),
                )
                self._append_orphan_event(
                    conn,
                    task_id=task_id,
                    payload={
                        "orphan_kind": "metadata",
                        "call_id": call_id,
                        "path": str(path),
                        "detected_by": "retention_sweep",
                    },
                    created_at=now,
                )
            counts["stale_metadata"] += 1

        return counts

    def _resolve_db_path(self, log_path: str) -> Path:
        path = Path(log_path)
        return path if path.is_absolute() else self._log_base_dir / path

    def _iter_log_files(self) -> list[Path]:
        if not self._log_base_dir.exists():
            return []
        return [path for path in self._log_base_dir.rglob("*.jsonl.zst") if path.is_file()]

    def _append_orphan_event(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str | None,
        payload: dict[str, str],
        created_at: datetime,
    ) -> None:
        conn.execute(
            """
            INSERT INTO system_events(task_id, event_type, severity, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                task_id,
                "retention_orphan_detected",
                "warning",
                json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                created_at.isoformat(),
            ),
        )


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
