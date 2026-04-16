from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from task_relay.errors import (
    FAILURE_CLASS,
    FailureClass,
    FailureCode,
    TimeoutTransportError,
    TransportError,
)
from task_relay.types import AdapterContract


@dataclass(frozen=True)
class AdapterOutput:
    ok: bool
    payload: dict[str, Any]
    failure_code: FailureCode | None
    tokens_in: int | None
    tokens_out: int | None
    raw_text: str | None


class TimeoutDecision(str, Enum):
    RETRY = "retry"
    GIVE_UP_HR = "give_up_hr"
    GIVE_UP_NEEDS_FIX = "give_up_needs_fix"


class AdapterTransport(Protocol):
    def request(self, *, request_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        ...


class AdapterBase:
    contract: AdapterContract

    def __init__(
        self,
        transport: AdapterTransport,
        *,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._sleep = _sleep

    def call(self, *, request_id: str, payload: dict[str, Any]) -> AdapterOutput:
        attempt_count = 0

        while True:
            attempt_count += 1
            transport_request_id = request_id if self.contract.supports_request_id else None
            request_payload = dict(payload)
            request_payload.pop("request_id", None)
            if self.contract.supports_request_id:
                request_payload["request_id"] = request_id

            try:
                response = self._transport.request(
                    request_id=transport_request_id,
                    payload=request_payload,
                )
            except TimeoutTransportError:
                raise
            except TransportError as exc:
                failure_class = FAILURE_CLASS[exc.failure_code]
                if failure_class == FailureClass.FATAL:
                    return self._failure_output(exc)
                if failure_class == FailureClass.TRANSIENT:
                    if attempt_count >= 3:
                        return self._failure_output(exc)
                    self._sleep(2 ** (attempt_count - 1))
                    continue
                if attempt_count >= 2:
                    return self._failure_output(exc)
                continue

            return self._success_output(response)

    def _failure_output(self, error: TransportError) -> AdapterOutput:
        raw_text = error.raw_text
        if raw_text is None and error.args:
            raw_text = str(error)
        return AdapterOutput(
            ok=False,
            payload={},
            failure_code=error.failure_code,
            tokens_in=None,
            tokens_out=None,
            raw_text=raw_text,
        )

    def _success_output(self, response: dict[str, Any]) -> AdapterOutput:
        payload = response.get("payload")
        if payload is None:
            payload = {
                key: value
                for key, value in response.items()
                if key not in {"tokens_in", "tokens_out", "raw_text"}
            }
        if not isinstance(payload, dict):
            return AdapterOutput(
                ok=False,
                payload={},
                failure_code=FailureCode.ADAPTER_PARSE_ERROR,
                tokens_in=None,
                tokens_out=None,
                raw_text=self._coerce_raw_text(response.get("raw_text")),
            )
        return AdapterOutput(
            ok=True,
            payload=payload,
            failure_code=None,
            tokens_in=self._coerce_int(response.get("tokens_in")),
            tokens_out=self._coerce_int(response.get("tokens_out")),
            raw_text=self._coerce_raw_text(response.get("raw_text")),
        )

    def _coerce_int(self, value: Any) -> int | None:
        return value if isinstance(value, int) else None

    def _coerce_raw_text(self, value: Any) -> str | None:
        return value if isinstance(value, str) else None
