from __future__ import annotations

from task_relay.types import OutboxRecord


class LoggingSink:
    def __init__(self) -> None:
        self.records: list[OutboxRecord] = []

    def send(self, record: OutboxRecord) -> None:
        self.records.append(record)
