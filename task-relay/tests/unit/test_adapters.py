from __future__ import annotations

from task_relay.runner.adapters.executor import check_file_scope
from task_relay.runner.adapters.planner import validate_plan
from task_relay.runner.adapters.reviewer import summarize_review


def test_validate_plan_full_score() -> None:
    plan = {
        "goal": "Implement planner validation",
        "sub_tasks": ["Add validator", "Write tests"],
        "allowed_files": ["src/task_relay/runner/**/*.py"],
        "auto_allowed_patterns": ["**/.pytest_cache/**"],
        "acceptance_criteria": ["Validator returns deterministic score"],
        "forbidden_changes": ["Do not modify router files"],
        "risk_notes": ["Schema drift with planner output"],
    }

    assert validate_plan(plan) == (100, 0)


def test_validate_plan_missing_keys_reduces_score_and_counts_errors() -> None:
    plan = {
        "goal": "",
        "sub_tasks": "not-a-list",
        "allowed_files": [],
        "auto_allowed_patterns": [],
        "acceptance_criteria": [],
        "risk_notes": ["Keep DB untouched"],
    }

    score, errors = validate_plan(plan)

    assert score == 10
    assert errors == 2


def test_check_file_scope_matches_recursive_glob() -> None:
    in_scope, out_of_scope = check_file_scope(
        changed_files=["src/a/b.py", "docs/x.md"],
        allowed_files=["src/**/*.py"],
        auto_allowed_patterns=[],
    )

    assert in_scope == ["src/a/b.py"]
    assert out_of_scope == ["docs/x.md"]


def test_summarize_review_downgrades_empty_evidence_to_unchecked() -> None:
    summary = summarize_review(
        {
            "criteria": [
                {
                    "criterion_id": "c1",
                    "status": "satisfied",
                    "evidence_refs": [],
                },
                {
                    "criterion_id": "c2",
                    "status": "unsatisfied",
                    "evidence_refs": ["tests::test_case"],
                },
            ],
            "decision": "pass",
            "policy_breaches": [],
            "extra_files": [],
        }
    )

    assert summary["unchecked_count"] == 1
    assert summary["unsatisfied_count"] == 1
    assert summary["decision"] == "pass"
