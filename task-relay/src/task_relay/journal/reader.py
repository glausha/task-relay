"""Ingress journal reader for detailed-design §2.2."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import zstandard

from task_relay.types import CanonicalEvent, JournalPosition
from task_relay.types import Source


class JournalReader:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def iterate_from(
        self,
        position: JournalPosition | None,
    ) -> Iterator[tuple[JournalPosition, CanonicalEvent]]:
        files = sorted(self._base_dir.glob("*.ndjson.zst"))
        if position is None:
            start_name = None
            start_offset = 0
        else:
            start_name = position.file
            start_offset = position.offset
        for path in files:
            if start_name is not None and path.name < start_name:
                continue
            offset = start_offset if path.name == start_name else 0
            yield from _iterate_file(path, offset)


def _iterate_file(path: Path, start_offset: int) -> Iterator[tuple[JournalPosition, CanonicalEvent]]:
    with path.open("rb") as journal_file:
        compressed = journal_file.read()
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(compressed)) as reader:
        content = reader.read()
    cursor = start_offset
    while cursor < len(content):
        line_end = content.find(b"\n", cursor)
        if line_end < 0:
            break
        next_offset = line_end + 1
        event = _parse_event(content[cursor:line_end])
        yield JournalPosition(file=path.name, offset=next_offset), event
        cursor = next_offset


def _parse_event(line: bytes) -> CanonicalEvent:
    payload = json.loads(line.decode("utf-8"))
    return CanonicalEvent(
        event_id=str(payload["event_id"]),
        source=Source(payload["source"]),
        delivery_id=str(payload["delivery_id"]),
        event_type=str(payload["event_type"]),
        payload=dict(payload["payload"]),
        received_at=_parse_datetime(str(payload["received_at"])),
        request_id=payload["request_id"],
    )


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
