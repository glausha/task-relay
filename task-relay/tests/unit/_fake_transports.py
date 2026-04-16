from __future__ import annotations

from typing import Any


class FakeTransport:
    def __init__(
        self,
        responses: list[dict[str, Any]],
        *,
        errors: list[Exception | None] | None = None,
    ) -> None:
        self._responses = [dict(response) for response in responses]
        self._errors = list(errors or [])
        self._calls: list[tuple[str | None, dict[str, Any]]] = []

    def request(self, *, request_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        call_index = len(self._calls)
        self._calls.append((request_id, dict(payload)))
        error = self._errors[call_index] if call_index < len(self._errors) else None
        if error is not None:
            raise error
        if call_index >= len(self._responses):
            raise AssertionError(f"Missing fake response for call {call_index}")
        return dict(self._responses[call_index])

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [dict(payload) for _, payload in self._calls]

    @property
    def request_ids(self) -> list[str | None]:
        return [request_id for request_id, _ in self._calls]
