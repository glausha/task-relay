from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from task_relay.clock import FrozenClock
from task_relay.db.connection import connect
from task_relay.ingester.journal_ingester import JournalIngester
from task_relay.journal.reader import JournalReader
from task_relay.journal.writer import JournalWriter
from task_relay.types import CanonicalEvent, JournalPosition, Source


def _patch_journal_io(monkeypatch) -> None:
    def append(self, event: CanonicalEvent):
        self._base_dir.mkdir(parents=True, exist_ok=True)
        path = self._base_dir / f"{event.received_at:%Y%m%d}.jsonl"
        record = {
            "event_id": event.event_id,
            "source": event.source.value,
            "delivery_id": event.delivery_id,
            "event_type": event.event_type,
            "payload": event.payload,
            "received_at": event.received_at.isoformat(),
            "request_id": event.request_id,
        }
        data = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        with path.open("ab") as fh:
            fh.write(data)
            return JournalPosition(file=path.name, offset=fh.tell())

    def iterate_from(self, position):
        self._base_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(self._base_dir.glob("*.jsonl"))
        start = position is None
        for path in files:
            if not start:
                if path.name < position.file:
                    continue
                start = True
            with path.open("rb") as fh:
                if position is not None and path.name == position.file:
                    fh.seek(position.offset)
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    payload = json.loads(line)
                    yield (
                        JournalPosition(file=path.name, offset=fh.tell()),
                        CanonicalEvent(
                            event_id=payload["event_id"],
                            source=Source(payload["source"]),
                            delivery_id=payload["delivery_id"],
                            event_type=payload["event_type"],
                            payload=payload["payload"],
                            received_at=datetime.fromisoformat(payload["received_at"]),
                            request_id=payload["request_id"],
                        ),
                    )

    monkeypatch.setattr(JournalWriter, "append", append)
    monkeypatch.setattr(JournalReader, "iterate_from", iterate_from)


def _event(event_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        source=Source.CLI,
        delivery_id=f"delivery-{event_id}",
        event_type="issue_comment.created",
        payload={"task_id": "task-1", "n": event_id},
        received_at=datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc),
        request_id=None,
    )


def _conn_factory(sqlite_conn):
    row = sqlite_conn.execute("PRAGMA database_list").fetchone()
    db_path = Path(row["file"])

    def factory():
        conn = connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return factory


def test_ingester_step_imports_three_events(sqlite_conn, tmp_path, monkeypatch) -> None:
    _patch_journal_io(monkeypatch)
    writer = JournalWriter(tmp_path)
    for idx in range(3):
        writer.append(_event(f"evt-{idx}"))

    ingester = JournalIngester(
        conn_factory=_conn_factory(sqlite_conn),
        journal_reader=JournalReader(tmp_path),
        clock=FrozenClock(datetime(2026, 4, 15, 0, 1, tzinfo=timezone.utc)),
    )

    count = ingester.step()

    rows = sqlite_conn.execute("SELECT event_id FROM event_inbox ORDER BY event_id").fetchall()
    assert count == 3
    assert [row["event_id"] for row in rows] == ["evt-0", "evt-1", "evt-2"]


def test_ingester_dedups_when_rereading_same_event(sqlite_conn, tmp_path, monkeypatch) -> None:
    _patch_journal_io(monkeypatch)
    writer = JournalWriter(tmp_path)
    writer.append(_event("evt-dup"))

    ingester = JournalIngester(
        conn_factory=_conn_factory(sqlite_conn),
        journal_reader=JournalReader(tmp_path),
        clock=FrozenClock(datetime(2026, 4, 15, 0, 1, tzinfo=timezone.utc)),
    )

    first = ingester.step()
    sqlite_conn.execute(
        "UPDATE journal_ingester_state SET last_file = NULL, last_offset = 0 WHERE singleton_id = 1"
    )
    second = ingester.step()
    row = sqlite_conn.execute("SELECT COUNT(*) AS count FROM event_inbox WHERE event_id = 'evt-dup'").fetchone()

    assert first == 1
    assert second == 1
    assert row["count"] == 1
