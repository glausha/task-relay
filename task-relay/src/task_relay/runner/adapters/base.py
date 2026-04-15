from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
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


class TimeoutDecision(str, Enum):
    RETRY = "retry"
    GIVE_UP_HR = "give_up_hr"
    GIVE_UP_NEEDS_FIX = "give_up_needs_fix"


class AdapterBase(ABC):
    # True: timeout retry may reuse the same request_id (detailed-design §8.1).
    # False: planning/reviewing timeout auto-retry is forbidden and caller must fall back to human_review_required.
    # executing never allows timeout/oom_killed auto-retry regardless of this flag.
    # WHY: caller-side timeout handling depends on whether the adapter can safely reuse request_id.
    contract: AdapterContract

    @abstractmethod
    def call(self, *, request_id: str, payload: dict[str, Any]) -> AdapterOutput:
        raise NotImplementedError("Phase 1: runner adapter")
