from __future__ import annotations

import hmac
import json
from pathlib import Path
from typing import NamedTuple

import pytest
from aiohttp.test_utils import TestClient, TestServer

from task_relay.ingress.forgejo_server import ForgejoWebhookServer
from task_relay.journal.reader import JournalReader
from task_relay.journal.writer import JournalWriter
from task_relay.types import CanonicalEvent


class ResponseSnapshot(NamedTuple):
    status: int
    text: str


@pytest.mark.asyncio
async def test_forgejo_server_accepts_valid_webhook_and_appends_journal(tmp_path: Path) -> None:
    secret = b"top-secret"
    body = {
        "action": "opened",
        "issue": {"id": 10, "number": 7, "title": "Example"},
        "repository": {"id": 20, "full_name": "org/repo"},
        "sender": {"id": 30},
    }
    response, events = await _post_webhook(tmp_path, secret=secret, body=body, event="issues", delivery_id="delivery-1")

    assert response.status == 202
    assert response.text == "accepted"
    assert len(events) == 1


@pytest.mark.asyncio
async def test_forgejo_server_rejects_invalid_hmac(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal"
    writer = JournalWriter(journal_dir)
    server = ForgejoWebhookServer(writer, b"top-secret")
    client = TestClient(TestServer(server.create_app()))
    await client.start_server()
    try:
        response = await client.post(
            "/webhook/forgejo",
            data=b'{"action":"opened"}',
            headers={
                "X-Forgejo-Signature": "sha256=invalid",
                "X-Forgejo-Event": "issues",
                "X-Forgejo-Delivery": "delivery-2",
            },
        )
        response_text = await response.text()
        events = _read_journal_events(journal_dir)
    finally:
        await client.close()

    assert response.status == 401
    assert response_text == "invalid signature"
    assert events == []


@pytest.mark.asyncio
async def test_forgejo_server_ignores_unsupported_event(tmp_path: Path) -> None:
    body = {"action": "opened"}
    response, events = await _post_webhook(
        tmp_path,
        secret=b"top-secret",
        body=body,
        event="pull_request",
        delivery_id="delivery-3",
    )

    assert response.status == 200
    assert response.text == "ignored"
    assert events == []


async def _post_webhook(
    tmp_path: Path,
    *,
    secret: bytes,
    body: dict[str, object],
    event: str,
    delivery_id: str,
) -> tuple[ResponseSnapshot, list[CanonicalEvent]]:
    journal_dir = tmp_path / "journal"
    writer = JournalWriter(journal_dir)
    server = ForgejoWebhookServer(writer, secret)
    client = TestClient(TestServer(server.create_app()))
    await client.start_server()
    body_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signature = "sha256=" + hmac.new(secret, body_bytes, "sha256").hexdigest()
    try:
        response = await client.post(
            "/webhook/forgejo",
            data=body_bytes,
            headers={
                "X-Forgejo-Signature": signature,
                "X-Forgejo-Event": event,
                "X-Forgejo-Delivery": delivery_id,
            },
        )
        response_snapshot = ResponseSnapshot(status=response.status, text=await response.text())
        events = _read_journal_events(journal_dir)
    finally:
        await client.close()
    return response_snapshot, events


def _read_journal_events(journal_dir: Path) -> list[CanonicalEvent]:
    reader = JournalReader(journal_dir)
    return [event for _, event in reader.iterate_from(None)]
