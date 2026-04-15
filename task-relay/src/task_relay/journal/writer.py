"""Ingress journal writer for detailed-design §2.2."""

from __future__ import annotations

import io
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import BinaryIO

import zstandard

from task_relay.clock import Clock, SystemClock
from task_relay.errors import JournalError
from task_relay.journal.paths import daily_path

from task_relay.types import CanonicalEvent, JournalPosition


class JournalWriter:
    def __init__(self, base_dir: Path, clock: Clock = SystemClock()) -> None:
        self._base_dir = base_dir
        self._clock = clock
        self._compressor = zstandard.ZstdCompressor()
        self._current_day: date | None = None
        self._current_path: Path | None = None
        self._journal_file: BinaryIO | None = None
        self._offset = 0
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def append(self, event: CanonicalEvent) -> JournalPosition:
        day = self._clock.now().astimezone(timezone.utc).date()
        self._rotate_if_needed(day)
        if self._journal_file is None or self._current_path is None:
            raise JournalError("journal file is not open")
        payload = _serialize_event(event)
        position = JournalPosition(file=self._current_path.name, offset=self._offset)
        self._journal_file.write(self._compressor.compress(payload))
        self._journal_file.flush()
        os.fsync(self._journal_file.fileno())
        self._offset += len(payload)
        return position

    def close(self) -> None:
        if self._journal_file is None:
            return
        self._journal_file.close()
        self._journal_file = None
        self._current_day = None
        self._current_path = None
        self._offset = 0

    def _rotate_if_needed(self, day: date) -> None:
        if self._current_day == day and self._journal_file is not None:
            return
        self.close()
        path = daily_path(self._base_dir, day)
        self._current_day = day
        self._current_path = path
        self._offset = _existing_offset(path)
        self._journal_file = path.open("ab")


def _serialize_event(event: CanonicalEvent) -> bytes:
    body = json.dumps(
        {
            "event_id": event.event_id,
            "source": event.source.value,
            "delivery_id": event.delivery_id,
            "event_type": event.event_type,
            "payload": event.payload,
            "received_at": _to_iso(event.received_at),
            "request_id": event.request_id,
        },
        sort_keys=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{body}\n".encode("utf-8")


def _existing_offset(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("rb") as journal_file:
            compressed = journal_file.read()
        return len(_decompress_all(compressed))
    except zstandard.ZstdError as exc:
        raise JournalError(f"failed to read journal offset from {path}") from exc


def _decompress_all(data: bytes) -> bytes:
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as reader:
        return reader.read()


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
