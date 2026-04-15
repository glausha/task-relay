from __future__ import annotations

from datetime import date
from pathlib import Path


def daily_path(base: Path, day: date) -> Path:
    return base / f"{day.strftime('%Y%m%d')}.ndjson.zst"
