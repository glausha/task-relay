from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from task_relay.clock import Clock, SystemClock


RETAIN_DAYS_LOCAL = 30
RETAIN_DAYS_OFFSITE = 7


class JournalRetention:
    def __init__(self, journal_dir: Path, clock: Clock = SystemClock()) -> None:
        self._journal_dir = journal_dir
        self._clock = clock

    def sweep(self) -> int:
        if not self._journal_dir.exists():
            return 0
        cutoff_date = (self._clock.now() - timedelta(days=RETAIN_DAYS_LOCAL)).date()
        deleted = 0
        for path in self._journal_dir.glob("*.ndjson.zst"):
            try:
                file_date = datetime.strptime(path.stem.split(".")[0], "%Y%m%d").date()
            except ValueError:
                continue
            if file_date < cutoff_date and path.is_file():
                path.unlink()
                deleted += 1
        return deleted
