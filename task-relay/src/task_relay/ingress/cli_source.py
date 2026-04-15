from __future__ import annotations

import hashlib
from typing import Any

from task_relay import ids
from task_relay.clock import Clock, SystemClock
from task_relay.types import CanonicalEvent, Source


def cli_actor_principal(actor: str) -> str:
    return f"cli:{actor}"


def build_cli_event(
    *,
    event_type: str,
    task_id: str | None,
    actor: str,
    payload: dict[str, Any] | None = None,
    clock: Clock = SystemClock(),
) -> CanonicalEvent:
    received_at = clock.now()
    delivery_basis = "|".join([event_type, task_id or "", actor, received_at.isoformat()])
    merged_payload = dict(payload or {})
    merged_payload["actor"] = actor
    merged_payload["task_id"] = task_id
    return CanonicalEvent(
        event_id=ids.new_event_id(),
        source=Source.CLI,
        delivery_id=hashlib.sha256(delivery_basis.encode("utf-8")).hexdigest()[:32],
        event_type=event_type,
        payload=merged_payload,
        received_at=received_at,
        request_id=ids.new_request_id(),
    )


def build_ingress_issue_event(
    *,
    source: Source = Source.FORGEJO,
    event_type: str,
    delivery_id: str,
    payload: dict[str, Any],
    clock: Clock = SystemClock(),
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=ids.new_event_id(),
        source=source,
        delivery_id=delivery_id,
        event_type=event_type,
        payload=dict(payload),
        received_at=clock.now(),
        request_id=ids.new_request_id(),
    )


OWNER_OR_ADMIN_EVENTS = frozenset({"/approve", "/critical on", "/critical off", "/retry", "/cancel"})
ADMIN_ONLY_EVENTS = frozenset({"/unlock", "/retry-system"})


def is_authorized(
    event_type: str,
    actor_id: int,
    task_requested_by: str | None,
    admin_user_ids: list[int],
) -> bool:
    if event_type in OWNER_OR_ADMIN_EVENTS:
        return task_requested_by == f"discord:{actor_id}" or actor_id in admin_user_ids
    if event_type in ADMIN_ONLY_EVENTS:
        return actor_id in admin_user_ids
    return True
