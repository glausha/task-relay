from __future__ import annotations

from task_relay.runner.adapters.base import TimeoutDecision
from task_relay.runner.tool_runner import decide_timeout_retry
from task_relay.types import AdapterContract, Stage


def test_decide_timeout_retry_retries_planning_once_with_request_id_support() -> None:
    contract = AdapterContract("planner", "v1", True)

    assert decide_timeout_retry(stage=Stage.PLANNING, contract=contract, attempt_count=0) == TimeoutDecision.RETRY


def test_decide_timeout_retry_requires_human_review_without_request_id_support() -> None:
    contract = AdapterContract("planner", "v1", False)

    assert (
        decide_timeout_retry(stage=Stage.PLANNING, contract=contract, attempt_count=0)
        == TimeoutDecision.GIVE_UP_HR
    )


def test_decide_timeout_retry_requires_human_review_after_retry_budget_is_spent() -> None:
    contract = AdapterContract("planner", "v1", True)

    assert decide_timeout_retry(stage=Stage.PLANNING, contract=contract, attempt_count=1) == TimeoutDecision.GIVE_UP_HR


def test_decide_timeout_retry_requires_human_review_during_execution() -> None:
    contract = AdapterContract("executor", "v1", True)

    assert decide_timeout_retry(stage=Stage.EXECUTING, contract=contract, attempt_count=0) == TimeoutDecision.GIVE_UP_HR


def test_decide_timeout_retry_retries_reviewing_once_with_request_id_support() -> None:
    contract = AdapterContract("reviewer", "v1", True)

    assert decide_timeout_retry(stage=Stage.REVIEWING, contract=contract, attempt_count=0) == TimeoutDecision.RETRY
