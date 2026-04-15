from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from task_relay.clock import FrozenClock
from task_relay.ingress.cli_source import build_cli_event, is_authorized
from task_relay.ingress.forgejo_webhook import canonicalize, verify_signature
from task_relay.types import Source


def test_verify_signature_accepts_valid_hmac() -> None:
    body = b'{"action":"opened"}'
    secret = b"top-secret"
    signature = "sha256=" + hmac.new(secret, body, "sha256").hexdigest()
    assert verify_signature(body, signature, secret) is True


def test_verify_signature_rejects_tampered_body() -> None:
    body = b'{"action":"opened"}'
    secret = b"top-secret"
    signature = "sha256=" + hmac.new(secret, body, "sha256").hexdigest()
    assert verify_signature(b'{"action":"closed"}', signature, secret) is False


def test_canonicalize_maps_issues_opened() -> None:
    fixed = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    event = canonicalize(
        "issues",
        "delivery-1",
        {
            "action": "opened",
            "issue": {"id": 10, "number": 7, "title": "Example"},
            "repository": {"id": 20, "full_name": "org/repo"},
            "sender": {"id": 30},
        },
        clock=FrozenClock(fixed),
    )
    assert event is not None
    assert event.source is Source.FORGEJO
    assert event.delivery_id == "delivery-1"
    assert event.event_type == "issues.opened"
    assert event.payload["issue_number"] == 7
    assert event.received_at == fixed


def test_canonicalize_ignores_unsupported_events() -> None:
    event = canonicalize("pull_request", "delivery-2", {"action": "opened"})
    assert event is None


def test_build_cli_event_uses_deterministic_delivery_id() -> None:
    fixed = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    clock = FrozenClock(fixed)
    event_a = build_cli_event(
        event_type="/approve",
        task_id="task-1",
        actor="42",
        payload={"mode": "manual"},
        clock=clock,
    )
    event_b = build_cli_event(
        event_type="/approve",
        task_id="task-1",
        actor="42",
        payload={"mode": "manual"},
        clock=clock,
    )
    expected = hashlib.sha256("/approve|task-1|42|2026-04-15T12:00:00+00:00".encode("utf-8")).hexdigest()[:32]
    assert event_a.delivery_id == expected
    assert event_b.delivery_id == expected
    assert event_a.payload == {"mode": "manual", "actor": "42", "task_id": "task-1"}


def test_is_authorized_accepts_request_owner() -> None:
    assert is_authorized("/approve", 42, "42", [7]) is True


def test_is_authorized_rejects_non_admin_non_owner() -> None:
    assert is_authorized("/retry", 42, "99", [7]) is False


def test_is_authorized_rejects_unlock_for_non_admin() -> None:
    assert is_authorized("/unlock", 42, "42", [7]) is False
