from __future__ import annotations

from typing import Any

from task_relay.runner.adapters.base import AdapterBase, AdapterOutput
from task_relay.types import AdapterContract, CriterionStatus, ReviewDecision


class ReviewerAdapter(AdapterBase):
    contract = AdapterContract("reviewer", "v1", True)

    def call(self, *, request_id: str, payload: dict[str, Any]) -> AdapterOutput:
        raise NotImplementedError("Phase 2: reviewer LLM integration")


def summarize_review(review_json: dict[str, Any]) -> dict[str, Any]:
    criteria = review_json.get("criteria")
    unchecked_count = 0
    unsatisfied_count = 0

    if isinstance(criteria, list):
        for criterion in criteria:
            if not isinstance(criterion, dict):
                unchecked_count += 1
                continue
            evidence_refs = criterion.get("evidence_refs")
            has_evidence = isinstance(evidence_refs, list) and len(evidence_refs) > 0
            status = criterion.get("status")
            if not has_evidence:
                unchecked_count += 1
                continue
            if status == CriterionStatus.UNSATISFIED.value:
                unsatisfied_count += 1
            elif status != CriterionStatus.SATISFIED.value:
                unchecked_count += 1

    decision = review_json.get("decision")
    if not isinstance(decision, str):
        decision = ReviewDecision.HUMAN_REVIEW_REQUIRED.value

    return {
        "unchecked_count": unchecked_count,
        "unsatisfied_count": unsatisfied_count,
        "decision": decision,
        "policy_breaches": _string_list(review_json.get("policy_breaches")),
        "extra_files": _string_list(review_json.get("extra_files")),
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]
