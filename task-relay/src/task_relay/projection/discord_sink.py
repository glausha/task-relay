from __future__ import annotations

import asyncio

import discord

from task_relay.types import OutboxRecord
from task_relay.types import Stream


class DiscordSink:
    ADMIN_USER_IDS_SENTINEL = "admin_user_ids"

    def __init__(
        self,
        *,
        client: discord.Client | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        admin_user_ids: list[int] | None = None,
    ) -> None:
        self._client = client
        self._loop = loop
        self._admin_user_ids = [] if admin_user_ids is None else list(admin_user_ids)

    def send(self, record: OutboxRecord) -> None:
        if record.stream is not Stream.DISCORD_ALERT:
            raise ValueError(f"discord sink does not support stream={record.stream.value}")
        recipients = self._resolve_recipients(record.target)
        message = self._build_message(record)
        for user_id in recipients:
            self._send_dm(user_id, message)

    def _resolve_recipients(self, target: str) -> list[int]:
        if target == self.ADMIN_USER_IDS_SENTINEL:
            return list(self._admin_user_ids)
        try:
            return [int(target)]
        except ValueError:
            return list(self._admin_user_ids)

    def _build_message(self, record: OutboxRecord) -> str:
        payload = record.payload
        parts = [
            f"**{payload.get('kind', 'alert')}** - task `{record.task_id}`",
            f"state: `{payload.get('state', '?')}`",
        ]
        if task_url := payload.get("task_url"):
            parts.append(f"<{task_url}>")
        parts.append(f"`relay_idempotency_key={record.idempotency_key}`")
        return "\n".join(parts)

    def _send_dm(self, user_id: int, message: str) -> None:
        if self._client is None or self._loop is None:
            raise NotImplementedError("Phase 3: discord client not injected")
        future = asyncio.run_coroutine_threadsafe(self._async_send_dm(user_id, message), self._loop)
        future.result(timeout=10)

    async def _async_send_dm(self, user_id: int, message: str) -> None:
        if self._client is None:
            raise NotImplementedError("Phase 3: discord client not injected")
        user = await self._client.fetch_user(user_id)
        dm = await user.create_dm()
        await dm.send(message)


ADMIN_USER_IDS_SENTINEL = DiscordSink.ADMIN_USER_IDS_SENTINEL
