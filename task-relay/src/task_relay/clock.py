"""Clock abstraction: basic-design §4.1, detailed-design §4.1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class FrozenClock:
    fixed: datetime

    def now(self) -> datetime:
        return self.fixed
