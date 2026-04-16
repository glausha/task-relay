from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from task_relay.runner.adapters.base import AdapterBase, AdapterOutput, AdapterTransport
from task_relay.types import AdapterContract


class PlannerAdapter(AdapterBase):
    contract = AdapterContract("planner", "v1", True)

    def __init__(self, transport: AdapterTransport, *, sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(transport=transport, _sleep=sleep)

    def call(self, *, request_id: str, payload: dict[str, Any]) -> AdapterOutput:
        result = super().call(request_id=request_id, payload=payload)
        if not result.ok:
            return result
        score, errors = validate_plan(result.payload)
        return replace(
            result,
            payload={
                **result.payload,
                "validator_score": score,
                "validator_errors": errors,
            },
        )


def validate_plan(plan_json: dict[str, Any]) -> tuple[int, int]:
    required_keys = {
        "goal",
        "sub_tasks",
        "allowed_files",
        "auto_allowed_patterns",
        "acceptance_criteria",
        "forbidden_changes",
        "risk_notes",
    }
    score = 0
    errors = 0

    missing_keys = {key for key in required_keys if key not in plan_json}
    errors += len(missing_keys)

    goal = plan_json.get("goal")
    sub_tasks = plan_json.get("sub_tasks")
    allowed_files = plan_json.get("allowed_files")
    auto_allowed_patterns = plan_json.get("auto_allowed_patterns")
    acceptance_criteria = plan_json.get("acceptance_criteria")
    forbidden_changes = plan_json.get("forbidden_changes")
    risk_notes = plan_json.get("risk_notes")

    list_error_keys = (
        "sub_tasks",
        "acceptance_criteria",
        "forbidden_changes",
        "risk_notes",
    )
    for key in list_error_keys:
        if key in plan_json and not isinstance(plan_json.get(key), list):
            errors += 1

    if isinstance(goal, str) and goal.strip():
        score += 10

    if isinstance(sub_tasks, list) and sub_tasks and all(_is_non_empty_text(item) for item in sub_tasks):
        score += 15

    if _non_empty_list(allowed_files) or _non_empty_list(auto_allowed_patterns):
        score += 20

    if (
        isinstance(acceptance_criteria, list)
        and acceptance_criteria
        and all(_is_non_empty_text(item) for item in acceptance_criteria)
    ):
        score += 25

    if isinstance(forbidden_changes, list) and any(_is_non_empty_text(item) for item in forbidden_changes):
        score += 10

    if isinstance(risk_notes, list) and any(_is_non_empty_text(item) for item in risk_notes):
        score += 10

    if (
        not missing_keys
        and isinstance(goal, str)
        and isinstance(sub_tasks, list)
        and isinstance(allowed_files, list)
        and isinstance(auto_allowed_patterns, list)
        and isinstance(acceptance_criteria, list)
        and isinstance(forbidden_changes, list)
        and isinstance(risk_notes, list)
    ):
        score += 10

    return score, errors


def _is_non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _non_empty_list(value: Any) -> bool:
    return isinstance(value, list) and any(_is_non_empty_text(item) for item in value)
