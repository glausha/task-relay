from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from task_relay.errors import FailureCode
from task_relay.types import AdapterContract


@dataclass(frozen=True)
class AdapterOutput:
    ok: bool
    payload: dict[str, Any]
    failure_code: FailureCode | None
    tokens_in: int | None
    tokens_out: int | None
    raw_text: str | None


class AdapterBase(ABC):
    contract: AdapterContract

    @abstractmethod
    def call(self, *, request_id: str, payload: dict[str, Any]) -> AdapterOutput:
        raise NotImplementedError("Phase 1: runner adapter")
