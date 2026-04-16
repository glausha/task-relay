from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import timezone
from importlib import import_module

from task_relay.clock import Clock, SystemClock
from task_relay.journal.reader import JournalReader
from task_relay.types import InboxEvent, JournalPosition


class JournalIngester:
    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        journal_reader: JournalReader,
        clock: Clock = SystemClock(),
    ) -> None:
        self._conn_factory = conn_factory
        self._journal_reader = journal_reader
        self._clock = clock

    def step(self) -> int:
        queries = import_module("task_relay.db.queries")
        conn = self._conn_factory()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO journal_ingester_state(singleton_id, last_file, last_offset, updated_at)
                VALUES (1, NULL, 0, ?)
                """,
                (self._clock.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),),
            )
            last_file, last_offset = queries.get_ingester_state(conn)
            position = None if last_file is None else JournalPosition(file=last_file, offset=last_offset)
            count = 0
            for next_position, event in self._journal_reader.iterate_from(position):  # type: ignore[arg-type]
                inbox_event = InboxEvent(
                    event_id=event.event_id,
                    source=event.source,
                    delivery_id=event.delivery_id,
                    event_type=event.event_type,
                    payload=event.payload,
                    journal_offset=next_position.offset,
                    received_at=event.received_at,
                )
                conn.execute("BEGIN IMMEDIATE")
                try:
                    queries.insert_event(conn, inbox_event)
                    queries.update_ingester_state(
                        conn,
                        next_position.file,
                        next_position.offset,
                        self._clock.now(),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                count += 1
            return count
        finally:
            conn.close()

    def run_forever(self, poll_interval_sec: float = 1.0) -> None:
        while True:
            imported = self.step()
            if imported == 0:
                time.sleep(poll_interval_sec)
