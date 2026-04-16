from __future__ import annotations

import json
import subprocess
from typing import Any

from task_relay.errors import FailureCode, TimeoutTransportError, UnknownTransportError


class CodexTransport:
    """Codex CLI transport for Reviewer: detailed-design §6.4."""

    def __init__(self, *, model: str = "gpt-5.4", sandbox: str = "read-only") -> None:
        self._model = model
        self._sandbox = sandbox

    def request(self, *, request_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        del request_id
        prompt = json.dumps(payload)
        try:
            result = subprocess.run(
                ["codex", "exec", "-s", self._sandbox, "--model", self._model, "-"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutTransportError(raw_text=prompt) from exc
        except OSError as exc:
            raise UnknownTransportError(
                FailureCode.TOOL_INTERNAL_ERROR,
                str(exc),
                raw_text=str(exc),
            ) from exc
        if result.returncode != 0:
            raise UnknownTransportError(
                FailureCode.TOOL_INTERNAL_ERROR,
                raw_text=result.stderr[:500] or result.stdout[:500],
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise UnknownTransportError(
                FailureCode.INVALID_REVIEW_OUTPUT,
                raw_text=result.stdout[:500],
            ) from exc
        if not isinstance(payload, dict):
            raise UnknownTransportError(
                FailureCode.INVALID_REVIEW_OUTPUT,
                raw_text=result.stdout[:500],
            )
        return payload
