from __future__ import annotations

from typing import Any

import httpx

from task_relay.types import OutboxRecord
from task_relay.types import Stream


class DiscordSink:
    def __init__(self, base_url: str = "https://discord.com/api/v10", client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client()

    def send(self, record: OutboxRecord) -> None:
        if record.stream is not Stream.DISCORD_ALERT:
            raise ValueError(f"discord sink does not support stream={record.stream.value}")
        footer = f"relay_idempotency_key={record.idempotency_key}"
        content = f"{record.payload['content']}\n\n{footer}"
        url = f"{self._base_url}/channels/{record.target}/messages"
        self._request("POST", url, {"content": content})

    def _request(self, method: str, url: str, json: dict[str, Any]) -> dict[str, Any]:
        _ = (self._client, method, url, json)
        raise NotImplementedError("Phase 2 integration")
