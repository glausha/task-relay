from __future__ import annotations

import hmac
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from task_relay import ids
from task_relay.clock import Clock, SystemClock
from task_relay.types import CanonicalEvent, Source


ALLOWED_ISSUE_ACTIONS = frozenset({"opened", "closed", "reopened", "label_updated"})
SLASH_COMMANDS = {
    "/approve": "/approve",
    "/retry": "/retry",
    "/cancel": "/cancel",
    "/critical on": "/critical on",
    "/critical off": "/critical off",
    "/unlock": "/unlock",
    "/retry-system": "/retry-system",
}


def verify_signature(body_bytes: bytes, signature_header: str, secret: bytes) -> bool:
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    actual = hmac.new(secret, body_bytes, "sha256").hexdigest()
    return hmac.compare_digest(signature_header[len(prefix) :], actual)


def canonicalize(
    event_name: str,
    delivery_id: str,
    body_json: dict[str, Any],
    clock: Clock = SystemClock(),
) -> CanonicalEvent | None:
    received_at = clock.now()
    if event_name == "issues":
        return _canonicalize_issues(delivery_id=delivery_id, body_json=body_json, received_at=received_at)
    if event_name == "issue_comment":
        return _canonicalize_issue_comment(
            delivery_id=delivery_id,
            body_json=body_json,
            received_at=received_at,
        )
    return None


def _canonicalize_issues(
    *,
    delivery_id: str,
    body_json: dict[str, Any],
    received_at: datetime,
) -> CanonicalEvent | None:
    action = str(body_json.get("action", ""))
    if action not in ALLOWED_ISSUE_ACTIONS:
        return None
    payload = _base_payload(body_json)
    payload["action"] = action
    return CanonicalEvent(
        event_id=ids.new_event_id(),
        source=Source.FORGEJO,
        delivery_id=delivery_id,
        event_type=f"issues.{action}",
        payload=payload,
        received_at=received_at,
        request_id=None,
    )


def _canonicalize_issue_comment(
    *,
    delivery_id: str,
    body_json: dict[str, Any],
    received_at: datetime,
) -> CanonicalEvent | None:
    if str(body_json.get("action", "")) != "created":
        return None
    comment = _mapping(body_json.get("comment"))
    command = _parse_slash_command(comment.get("body"))
    if command is None:
        return None
    payload = _base_payload(body_json)
    payload["actor"] = str(_mapping(body_json.get("sender")).get("id", ""))
    payload["comment_id"] = comment.get("id")
    payload["comment_body"] = comment.get("body")
    return CanonicalEvent(
        event_id=ids.new_event_id(),
        source=Source.FORGEJO,
        delivery_id=delivery_id,
        event_type=command,
        payload=payload,
        received_at=received_at,
        request_id=ids.new_request_id(),
    )


def _parse_slash_command(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return SLASH_COMMANDS.get(" ".join(value.strip().split()).lower())


def _base_payload(body_json: dict[str, Any]) -> dict[str, Any]:
    issue = _mapping(body_json.get("issue"))
    repository = _mapping(body_json.get("repository"))
    sender = _mapping(body_json.get("sender"))
    task = _mapping(issue.get("task"))
    return {
        "task_id": issue.get("task_id") or task.get("id"),
        "issue_id": issue.get("id"),
        "issue_number": issue.get("number"),
        "issue_title": issue.get("title"),
        "repository_id": repository.get("id"),
        "repository_name": repository.get("full_name"),
        "sender_id": sender.get("id"),
        "sender_login": sender.get("login"),
        "raw": body_json,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}
