from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from task_relay.journal.reader import JournalReader
from task_relay.journal.writer import JournalWriter
from task_relay.types import CanonicalEvent, Source


@dataclass
class MutableClock:
    current: datetime

    def now(self) -> datetime:
        return self.current


def test_writer_and_reader_round_trip(tmp_path) -> None:
    clock = MutableClock(datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc))
    writer = JournalWriter(tmp_path, clock=clock)
    event = CanonicalEvent(
        event_id="evt-1",
        source=Source.DISCORD,
        delivery_id="delivery-1",
        event_type="task.created",
        payload={"task": "demo"},
        received_at=clock.now(),
        request_id="req-1",
    )

    writer.append(event)
    writer.close()

    items = list(JournalReader(tmp_path).iterate_from(None))

    assert len(items) == 1
    assert items[0][0].file == "20260415.ndjson.zst"
    assert items[0][0].offset > 0
    assert items[0][1] == event


def test_reader_spans_daily_rotation(tmp_path) -> None:
    clock = MutableClock(datetime(2026, 4, 15, 23, 59, tzinfo=timezone.utc))
    writer = JournalWriter(tmp_path, clock=clock)
    first = CanonicalEvent(
        event_id="evt-1",
        source=Source.CLI,
        delivery_id="delivery-1",
        event_type="task.created",
        payload={"n": 1},
        received_at=clock.now(),
        request_id=None,
    )
    second_time = clock.now() + timedelta(minutes=2)
    second = CanonicalEvent(
        event_id="evt-2",
        source=Source.INTERNAL,
        delivery_id="delivery-2",
        event_type="task.updated",
        payload={"n": 2},
        received_at=second_time,
        request_id="req-2",
    )

    writer.append(first)
    clock.current = second_time
    writer.append(second)
    writer.close()

    items = list(JournalReader(tmp_path).iterate_from(None))

    assert [event for _, event in items] == [first, second]
    assert [position.file for position, _ in items] == [
        "20260415.ndjson.zst",
        "20260416.ndjson.zst",
    ]


def test_reader_resumes_from_position(tmp_path) -> None:
    clock = MutableClock(datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc))
    writer = JournalWriter(tmp_path, clock=clock)
    events = [
        CanonicalEvent(
            event_id=f"evt-{index}",
            source=Source.FORGEJO,
            delivery_id=f"delivery-{index}",
            event_type="task.created",
            payload={"index": index},
            received_at=clock.now(),
            request_id=f"req-{index}",
        )
        for index in range(1, 4)
    ]

    for event in events:
        writer.append(event)
    writer.close()

    initial = list(JournalReader(tmp_path).iterate_from(None))
    resume_from = initial[0][0]
    resumed = list(JournalReader(tmp_path).iterate_from(resume_from))

    assert [event for _, event in resumed] == events[1:]
