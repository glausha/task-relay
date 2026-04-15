from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import zstandard

from task_relay.types import Stage


class LogWriter:
    def __init__(
        self,
        base_dir: Path,
        task_id: str,
        stage: Stage,
        call_id: str,
        started_at: datetime,
    ) -> None:
        started_utc = started_at.astimezone(timezone.utc)
        file_name = f"{started_utc.strftime('%Y%m%dT%H%M%SZ')}_{call_id}.jsonl.zst"
        self._path = base_dir / task_id / stage.value / file_name
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("wb")
        self._writer = zstandard.ZstdCompressor().stream_writer(self._file)
        self._closed_result: tuple[Path, str, int] | None = None

    def path(self) -> Path:
        return self._path

    def write_line(self, record: dict[str, Any]) -> None:
        if self._closed_result is not None:
            raise ValueError("log writer is closed")
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        self._writer.write(line.encode("utf-8"))

    def close(self) -> tuple[Path, str, int]:
        if self._closed_result is not None:
            return self._closed_result
        self._writer.close()
        self._file.close()
        digest = hashlib.sha256()
        with self._path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        size = self._path.stat().st_size
        self._closed_result = (self._path, digest.hexdigest(), size)
        return self._closed_result
