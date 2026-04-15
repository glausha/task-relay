from __future__ import annotations

from typing import Any

import httpx

from task_relay.types import OutboxRecord
from task_relay.types import Stream


ADMIN_USER_IDS_SENTINEL = "admin_user_ids"


class DiscordSink:
    def __init__(
        self,
        base_url: str = "https://discord.com/api/v10",
        client: httpx.Client | None = None,
        admin_user_ids: list[int] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client()
        self._admin_user_ids = [] if admin_user_ids is None else list(admin_user_ids)

    def send(self, record: OutboxRecord) -> None:
        if record.stream is not Stream.DISCORD_ALERT:
            raise ValueError(f"discord sink does not support stream={record.stream.value}")
        content = self._build_message(record)
        for recipient in self._resolve_recipients(record.target, self._admin_user_ids):
            url = f"{self._base_url}/channels/{recipient}/messages"
            self._request("POST", url, {"content": content})

    @staticmethod
    def _resolve_recipients(target: str, admin_user_ids: list[int]) -> list[int]:
        if target == ADMIN_USER_IDS_SENTINEL:
            return list(admin_user_ids)
        return [int(target)]

    def _build_message(self, record: OutboxRecord) -> str:
        payload = record.payload
        kind = str(payload.get("kind", "alert"))
        state = str(payload.get("state", "unknown"))
        task_id = str(payload.get("task_id", record.task_id))
        task_url = payload.get("task_url")
        lines = [
            f"[task-relay] {kind}",
            f"Task: {task_id}",
            f"State: {state}",
        ]
        if task_url is not None:
            lines.append(f"URL: {task_url}")
        lines.append("")
        lines.append(f"relay_idempotency_key={record.idempotency_key}")
        return "\n".join(lines)

    def _request(self, method: str, url: str, json: dict[str, Any]) -> dict[str, Any]:
        _ = (self._client, method, url, json)
        raise NotImplementedError("Phase 2 integration")
