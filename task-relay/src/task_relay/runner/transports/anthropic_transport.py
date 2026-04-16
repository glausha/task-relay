from __future__ import annotations

import json
from typing import Any

from task_relay.errors import (
    FailureCode,
    FatalTransportError,
    TimeoutTransportError,
    TransientTransportError,
    UnknownTransportError,
)

try:
    import anthropic
except ImportError as exc:  # pragma: no cover - exercised indirectly by environment setup
    anthropic = None
    _ANTHROPIC_IMPORT_ERROR = exc
else:
    _ANTHROPIC_IMPORT_ERROR = None


class AnthropicTransport:
    """Anthropic Messages API transport for Planner: detailed-design §6.1."""

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-6",
        max_tokens: int = 4096,
        system_prompt: str = "",
    ) -> None:
        if anthropic is None:
            raise RuntimeError("anthropic package is not installed") from _ANTHROPIC_IMPORT_ERROR
        self._client = anthropic.Anthropic()
        self._model = model
        self._max_tokens = max_tokens
        self._system = system_prompt

    def request(self, *, request_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        del request_id
        messages = [{"role": "user", "content": json.dumps(payload)}]
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=self._system,
                messages=messages,
            )
        except anthropic.RateLimitError as exc:
            raise TransientTransportError(FailureCode.RATE_LIMITED, str(exc)) from exc
        except anthropic.AuthenticationError as exc:
            raise FatalTransportError(FailureCode.AUTH_ERROR, str(exc)) from exc
        except anthropic.APITimeoutError as exc:
            raise TimeoutTransportError(str(exc)) from exc
        except anthropic.APIError as exc:
            raise UnknownTransportError(
                FailureCode.TOOL_INTERNAL_ERROR,
                str(exc),
                raw_text=str(exc),
            ) from exc

        text = ""
        if response.content:
            first_block = response.content[0]
            text = first_block.text if hasattr(first_block, "text") else ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UnknownTransportError(FailureCode.INVALID_PLAN_OUTPUT, raw_text=text) from exc
        if not isinstance(payload, dict):
            raise UnknownTransportError(FailureCode.INVALID_PLAN_OUTPUT, raw_text=text)
        return payload
