from __future__ import annotations

import pytest

from task_relay.errors import (
    FailureCode,
    FatalTransportError,
    TimeoutTransportError,
    TransientTransportError,
    UnknownTransportError,
)
from task_relay.runner.adapters.executor import ExecutorAdapter
from task_relay.runner.adapters.planner import PlannerAdapter
from task_relay.runner.adapters.reviewer import ReviewerAdapter
from tests.unit._fake_transports import FakeTransport


def test_planner_returns_validator_score_for_valid_plan() -> None:
    transport = FakeTransport([{"payload": _valid_plan_json()}])
    adapter = PlannerAdapter(transport, sleep=_no_sleep)

    result = adapter.call(request_id="req-1", payload={"prompt": "plan"})

    assert result.ok is True
    assert result.failure_code is None
    assert result.payload["validator_score"] == 100
    assert result.payload["validator_errors"] == 0


def test_planner_retries_transient_failures_until_success() -> None:
    transport = FakeTransport(
        [{}, {}, {"payload": _valid_plan_json()}],
        errors=[
            TransientTransportError(FailureCode.RATE_LIMITED),
            TransientTransportError(FailureCode.RATE_LIMITED),
            None,
        ],
    )
    adapter = PlannerAdapter(transport, sleep=_no_sleep)

    result = adapter.call(request_id="req-2", payload={"prompt": "plan"})

    assert result.ok is True
    assert transport.call_count == 3


def test_planner_returns_failure_after_transient_retry_budget_is_spent() -> None:
    transport = FakeTransport(
        [{}, {}, {}, {}],
        errors=[
            TransientTransportError(FailureCode.RATE_LIMITED),
            TransientTransportError(FailureCode.RATE_LIMITED),
            TransientTransportError(FailureCode.RATE_LIMITED),
            TransientTransportError(FailureCode.RATE_LIMITED),
        ],
    )
    adapter = PlannerAdapter(transport, sleep=_no_sleep)

    result = adapter.call(request_id="req-3", payload={"prompt": "plan"})

    assert result.ok is False
    assert result.failure_code == FailureCode.RATE_LIMITED
    assert transport.call_count == 3


def test_planner_returns_failure_immediately_for_fatal_error() -> None:
    transport = FakeTransport(
        [{}],
        errors=[FatalTransportError(FailureCode.AUTH_ERROR, "bad api key")],
    )
    adapter = PlannerAdapter(transport, sleep=_no_sleep)

    result = adapter.call(request_id="req-4", payload={"prompt": "plan"})

    assert result.ok is False
    assert result.failure_code == FailureCode.AUTH_ERROR
    assert transport.call_count == 1


def test_planner_retries_unknown_failure_once() -> None:
    transport = FakeTransport(
        [{}, {"payload": _valid_plan_json()}],
        errors=[UnknownTransportError(FailureCode.INVALID_PLAN_OUTPUT), None],
    )
    adapter = PlannerAdapter(transport, sleep=_no_sleep)

    result = adapter.call(request_id="req-5", payload={"prompt": "plan"})

    assert result.ok is True
    assert transport.call_count == 2


def test_planner_propagates_timeout_error() -> None:
    transport = FakeTransport([{}], errors=[TimeoutTransportError("timed out")])
    adapter = PlannerAdapter(transport, sleep=_no_sleep)

    with pytest.raises(TimeoutTransportError):
        adapter.call(request_id="req-6", payload={"prompt": "plan"})


def test_executor_marks_changed_files_in_scope() -> None:
    transport = FakeTransport(
        [
            {
                "payload": {
                    "changed_files": [
                        "src/task_relay/runner/adapters/base.py",
                        "tests/unit/test_adapter_runtime.py",
                    ]
                }
            }
        ]
    )
    adapter = ExecutorAdapter(transport, sleep=_no_sleep)

    result = adapter.call(
        request_id="req-7",
        payload={
            "allowed_files": ["src/task_relay/runner/**/*.py"],
            "auto_allowed_patterns": ["tests/unit/**/*.py"],
        },
    )

    assert result.ok is True
    assert result.payload["out_of_scope_files"] == []
    assert result.payload["in_scope_files"] == [
        "src/task_relay/runner/adapters/base.py",
        "tests/unit/test_adapter_runtime.py",
    ]


def test_executor_marks_out_of_scope_files() -> None:
    transport = FakeTransport([{"payload": {"changed_files": ["src/task_relay/runner/adapters/base.py", "docs/x.md"]}}])
    adapter = ExecutorAdapter(transport, sleep=_no_sleep)

    result = adapter.call(
        request_id="req-8",
        payload={
            "allowed_files": ["src/task_relay/runner/**/*.py"],
            "auto_allowed_patterns": [],
        },
    )

    assert result.ok is True
    assert result.payload["in_scope_files"] == ["src/task_relay/runner/adapters/base.py"]
    assert result.payload["out_of_scope_files"] == ["docs/x.md"]


def test_reviewer_normalizes_review_payload() -> None:
    transport = FakeTransport(
        [
            {
                "payload": {
                    "criteria": [
                        {
                            "criterion_id": "c1",
                            "status": "satisfied",
                            "evidence_refs": ["tests::test_case"],
                        }
                    ],
                    "decision": "pass",
                    "policy_breaches": [],
                    "extra_files": [],
                }
            }
        ]
    )
    adapter = ReviewerAdapter(transport, sleep=_no_sleep)

    result = adapter.call(request_id="req-9", payload={"prompt": "review"})

    assert result.ok is True
    assert result.payload["unchecked_count"] == 0
    assert result.payload["decision"] == "pass"


def test_planner_reuses_request_id_across_retries() -> None:
    transport = FakeTransport(
        [{}, {"payload": _valid_plan_json()}],
        errors=[TransientTransportError(FailureCode.RATE_LIMITED), None],
    )
    adapter = PlannerAdapter(transport, sleep=_no_sleep)

    result = adapter.call(request_id="req-10", payload={"prompt": "plan"})

    assert result.ok is True
    assert transport.request_ids == ["req-10", "req-10"]
    assert [payload["request_id"] for payload in transport.payloads] == ["req-10", "req-10"]


def test_executor_omits_request_id_when_contract_does_not_support_it() -> None:
    transport = FakeTransport(
        [{}, {"payload": {"changed_files": []}}],
        errors=[TransientTransportError(FailureCode.NETWORK_UNREACHABLE), None],
    )
    adapter = ExecutorAdapter(transport, sleep=_no_sleep)

    result = adapter.call(
        request_id="req-11",
        payload={
            "allowed_files": ["src/**/*.py"],
            "auto_allowed_patterns": [],
        },
    )

    assert result.ok is True
    assert transport.request_ids == [None, None]
    assert all("request_id" not in payload for payload in transport.payloads)


def _valid_plan_json() -> dict[str, object]:
    return {
        "goal": "Implement adapter runtime",
        "sub_tasks": ["Add runtime", "Add tests"],
        "allowed_files": ["src/task_relay/runner/**/*.py"],
        "auto_allowed_patterns": ["tests/unit/**/*.py"],
        "acceptance_criteria": ["Runtime retries known transient failures deterministically"],
        "forbidden_changes": ["Do not import external SDKs"],
        "risk_notes": ["Transport output shape can drift"],
    }


def _no_sleep(_: float) -> None:
    return None
