from __future__ import annotations

import json
from collections.abc import Iterable
from hashlib import sha256
from typing import Any


KEY_SEP = "\x1f"


def _digest(parts: Iterable[str]) -> str:
    return sha256(KEY_SEP.join(parts).encode("utf-8")).hexdigest()


def canonical_payload_sha256(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(serialized.encode("utf-8")).hexdigest()


def snapshot_key(task_id: str, target: str, state_rev: int, payload: dict[str, Any]) -> str:
    return _digest([task_id, "task_snapshot", target, str(state_rev), canonical_payload_sha256(payload)])


def comment_key(task_id: str, target: str, origin_event_id: str, comment_kind: str) -> str:
    return _digest([task_id, "task_comment", target, origin_event_id, comment_kind])


def label_sync_key(task_id: str, target: str, state_rev: int, desired_labels: Iterable[str]) -> str:
    return _digest([task_id, "task_label_sync", target, str(state_rev), ",".join(sorted(desired_labels))])


def discord_alert_key(task_id: str, target: str, alert_kind: str, state_rev: int) -> str:
    return _digest([task_id, "discord_alert", target, alert_kind, str(state_rev)])
