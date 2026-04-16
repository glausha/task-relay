from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from task_relay.projection.discord_sink import DiscordSink
from task_relay.types import OutboxRecord, Stream


def test_resolve_recipients_expands_admin_sentinel() -> None:
    sink = DiscordSink(admin_user_ids=[1, 2])
    assert sink._resolve_recipients("admin_user_ids") == [1, 2]


def test_resolve_recipients_keeps_direct_target() -> None:
    sink = DiscordSink(admin_user_ids=[1, 2])
    assert sink._resolve_recipients("42") == [42]


def test_resolve_recipients_falls_back_to_admins_for_invalid_target() -> None:
    sink = DiscordSink(admin_user_ids=[1, 2])
    assert sink._resolve_recipients("not-a-user-id") == [1, 2]


def test_build_message_includes_footer() -> None:
    sink = DiscordSink(admin_user_ids=[1, 2])
    record = OutboxRecord(
        outbox_id=1,
        task_id="task-1",
        stream=Stream.DISCORD_ALERT,
        target="42",
        origin_event_id="evt-1",
        payload={
            "kind": "human_review_required",
            "state": "human_review_required",
            "task_id": "task-1",
            "task_url": "https://forgejo.local/issues/42",
        },
        state_rev=3,
        idempotency_key="idem-1",
        attempt_count=0,
        next_attempt_at=datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc),
        sent_at=None,
    )

    message = sink._build_message(record)

    assert "human_review_required" in message
    assert "state: `human_review_required`" in message
    assert "<https://forgejo.local/issues/42>" in message
    assert "relay_idempotency_key=idem-1" in message


def test_send_without_client_raises_not_implemented() -> None:
    sink = DiscordSink(admin_user_ids=[1, 2])
    record = OutboxRecord(
        outbox_id=1,
        task_id="task-1",
        stream=Stream.DISCORD_ALERT,
        target="42",
        origin_event_id="evt-1",
        payload={"kind": "system_degraded", "state": "system_degraded"},
        state_rev=3,
        idempotency_key="idem-1",
        attempt_count=0,
        next_attempt_at=datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc),
        sent_at=None,
    )

    with pytest.raises(NotImplementedError, match="Phase 3: discord client not injected"):
        sink.send(record)


def test_send_dm_with_injected_client_uses_threadsafe_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    future = Mock()
    loop = Mock()
    captured: dict[str, object] = {}

    def fake_run_coroutine_threadsafe(coro, target_loop):
        captured["loop"] = target_loop
        captured["coro"] = coro
        coro.close()
        return future

    monkeypatch.setattr(
        "task_relay.projection.discord_sink.asyncio.run_coroutine_threadsafe",
        fake_run_coroutine_threadsafe,
    )
    sink = DiscordSink(client=Mock(), loop=loop, admin_user_ids=[1, 2])

    sink._send_dm(42, "hello")

    assert captured["loop"] is loop
    future.result.assert_called_once_with(timeout=10)
