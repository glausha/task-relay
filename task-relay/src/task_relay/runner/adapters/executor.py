from __future__ import annotations

import fnmatch
import time
from collections.abc import Iterable
from collections.abc import Callable
from dataclasses import replace
from functools import lru_cache
from typing import Any

from task_relay.runner.adapters.base import AdapterBase, AdapterOutput, AdapterTransport
from task_relay.types import AdapterContract


class ExecutorAdapter(AdapterBase):
    contract = AdapterContract("executor", "v1", False)

    def __init__(self, transport: AdapterTransport, *, sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(transport=transport, _sleep=sleep)

    def call(self, *, request_id: str, payload: dict[str, Any]) -> AdapterOutput:
        result = super().call(request_id=request_id, payload=payload)
        if not result.ok:
            return result
        changed = result.payload.get("changed_files", [])
        allowed = payload.get("allowed_files", [])
        auto = payload.get("auto_allowed_patterns", [])
        in_scope, out_of_scope = check_file_scope(changed, allowed, auto)
        return replace(
            result,
            payload={
                **result.payload,
                "in_scope_files": in_scope,
                "out_of_scope_files": out_of_scope,
            },
        )


def check_file_scope(
    changed_files: Iterable[str],
    allowed_files: Iterable[str],
    auto_allowed_patterns: Iterable[str],
) -> tuple[list[str], list[str]]:
    normalized_patterns = [
        pattern
        for pattern in (
            _normalize_pattern(item) for item in [*allowed_files, *auto_allowed_patterns]
        )
        if pattern is not None
    ]
    in_scope: list[str] = []
    out_of_scope: list[str] = []

    for changed_file in changed_files:
        normalized_path = _normalize_path(changed_file)
        if normalized_path is None:
            out_of_scope.append(str(changed_file))
            continue
        if any(_match_path(pattern, normalized_path) for pattern in normalized_patterns):
            in_scope.append(normalized_path)
        else:
            out_of_scope.append(normalized_path)

    return in_scope, out_of_scope


def _normalize_path(path: Any) -> str | None:
    if not isinstance(path, str):
        return None
    raw = path.replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        return None
    parts: list[str] = []
    for part in raw.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) if parts else None


def _normalize_pattern(pattern: Any) -> str | None:
    if not isinstance(pattern, str):
        return None
    raw = pattern.replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        return None
    parts: list[str] = []
    for part in raw.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) if parts else None


def _match_path(pattern: str, path: str) -> bool:
    pattern_parts = tuple(pattern.split("/"))
    path_parts = tuple(path.split("/"))
    return _match_parts(pattern_parts, path_parts)


@lru_cache(maxsize=2048)
def _match_parts(pattern_parts: tuple[str, ...], path_parts: tuple[str, ...]) -> bool:
    if not pattern_parts:
        return not path_parts
    head = pattern_parts[0]
    tail = pattern_parts[1:]
    if head == "**":
        if _match_parts(tail, path_parts):
            return True
        return bool(path_parts) and _match_parts(pattern_parts, path_parts[1:])
    if not path_parts:
        return False
    if not fnmatch.fnmatchcase(path_parts[0], head):
        return False
    return _match_parts(tail, path_parts[1:])
