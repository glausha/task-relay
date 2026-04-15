from __future__ import annotations

from datetime import datetime, timezone

from task_relay.projection.discord_sink import DiscordSink
from task_relay.types import OutboxRecord, Stream


def test_resolve_recipients_expands_admin_sentinel() -> None:
    assert DiscordSink._resolve_recipients("admin_user_ids", [1, 2]) == [1, 2]


def test_resolve_recipients_keeps_direct_target() -> None:
    assert DiscordSink._resolve_recipients("42", [1, 2]) == [42]


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
    assert "relay_idempotency_key=idem-1" in message
