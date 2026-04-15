from __future__ import annotations

import asyncio
from typing import Any
from collections.abc import Callable

from task_relay import ids
from task_relay.clock import Clock, SystemClock
from task_relay.errors import JournalError, TaskRelayError
from task_relay.ingress.cli_source import build_cli_event, is_authorized
from task_relay.journal.writer import JournalWriter
from task_relay.types import CanonicalEvent, JournalPosition


WRITER_QUEUE_CAPACITY = 1024
ACK_DEADLINE_MS = 1500


class DiscordWriterQueueFull(TaskRelayError):
    pass


class DiscordWriterTimeout(TaskRelayError):
    pass


class DiscordIngress:
    def __init__(
        self,
        journal_writer: JournalWriter,
        clock: Clock = SystemClock(),
        queue_capacity: int = WRITER_QUEUE_CAPACITY,
        ack_deadline_ms: int = ACK_DEADLINE_MS,
    ) -> None:
        self._journal_writer = journal_writer
        self._clock = clock
        self._ack_deadline_ms = ack_deadline_ms
        self._queue: asyncio.Queue[tuple[CanonicalEvent | None, asyncio.Future[JournalPosition] | None]] = (
            asyncio.Queue(maxsize=queue_capacity)
        )
        self._writer_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.create_task(self._writer_loop())

    async def stop(self) -> None:
        if self._writer_task is None:
            return
        await self._queue.put((None, None))
        await self._writer_task
        self._writer_task = None

    async def submit(self, event: CanonicalEvent) -> JournalPosition:
        if self._writer_task is None or self._writer_task.done():
            await self.start()
        loop = asyncio.get_running_loop()
        result: asyncio.Future[JournalPosition] = loop.create_future()
        try:
            self._queue.put_nowait((event, result))
        except asyncio.QueueFull as exc:
            raise DiscordWriterQueueFull("discord writer queue is full") from exc
        try:
            return await asyncio.wait_for(asyncio.shield(result), self._ack_deadline_ms / 1000)
        except asyncio.TimeoutError as exc:
            raise DiscordWriterTimeout("discord writer acknowledgement timed out") from exc

    async def handle_slash_command(
        self,
        *,
        command: str,
        user_id: int,
        task_id: str | None,
        extra_payload: dict[str, Any] | None,
        admin_user_ids: list[int],
        get_requested_by: Callable[[str], str | None],
    ) -> tuple[str, str | None]:
        request_id = ids.new_request_id()
        requested_by = get_requested_by(task_id) if task_id is not None else None
        if not is_authorized(command, user_id, requested_by, admin_user_ids):
            return (f"Unauthorized. request_id={request_id}", request_id)
        event = build_cli_event(
            event_type=command,
            task_id=task_id,
            actor=str(user_id),
            payload=extra_payload,
            clock=self._clock,
        )
        request_id = event.request_id
        try:
            await self.submit(event)
        except DiscordWriterQueueFull:
            return (
                f"Task relay is busy and could not accept this request. request_id={request_id}",
                request_id,
            )
        except DiscordWriterTimeout:
            return (
                f"Task relay did not confirm durable acceptance in time. request_id={request_id}",
                request_id,
            )
        except JournalError:
            return (
                f"Task relay could not durably record this request. request_id={request_id}",
                request_id,
            )
        return (f"Accepted. request_id={request_id}", request_id)

    async def _writer_loop(self) -> None:
        while True:
            event, result = await self._queue.get()
            try:
                if event is None or result is None:
                    return
                position = self._journal_writer.append(event)
                if not result.done():
                    result.set_result(position)
            except Exception as exc:
                if result is not None and not result.done():
                    result.set_exception(exc)
