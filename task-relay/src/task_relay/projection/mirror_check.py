"""Mirror readonly violation detection: detailed-design §3.1."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

import yaml

from task_relay.clock import Clock, SystemClock
from task_relay.system_events import append_system_event
from task_relay.types import Severity, SystemEventType

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---(?:\r?\n|$)", re.DOTALL)


def check_mirror_consistency(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    remote_body: str,
    expected_body: str,
    clock: Clock = SystemClock(),
) -> bool:
    """
    remote_body と expected_body を比較。frontmatter 部分が変更されていたら
    mirror_readonly_violation_detected を system_events に記録し False を返す。
    """
    remote_fm = _extract_frontmatter(remote_body)
    expected_fm = _extract_frontmatter(expected_body)
    if remote_fm == expected_fm:
        return True

    changed_fields = sorted(
        field
        for field in set(remote_fm) | set(expected_fm)
        if remote_fm.get(field) != expected_fm.get(field)
    )
    payload = json.dumps(
        {
            "changed_fields": changed_fields,
            "expected_frontmatter": expected_fm,
            "remote_frontmatter": remote_fm,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    # WHY: readonly mirror edits must leave an audit trail before relay overwrites them.
    append_system_event(
        conn,
        task_id=task_id,
        event_type=SystemEventType.MIRROR_READONLY_VIOLATION_DETECTED.value,
        severity=Severity.WARNING,
        payload_json=payload,
        created_at_iso=clock.now().isoformat(),
    )
    return False


def _extract_frontmatter(body: str) -> dict[str, Any]:
    """--- で囲まれた YAML frontmatter を parse。"""
    match = _FRONTMATTER_RE.match(body)
    if match is None:
        return {}
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    return {str(key): value for key, value in parsed.items()}
