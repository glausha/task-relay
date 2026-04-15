from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from task_relay.projection.labels import MANAGED_LABELS
from task_relay.types import OutboxRecord
from task_relay.types import Stream


class ForgejoSink:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client()

    def send(self, record: OutboxRecord) -> None:
        if record.stream is Stream.TASK_SNAPSHOT:
            issue_number = int(record.payload["issue_number"])
            url = f"{self._base_url}/api/v1/repos/{record.target}/issues/{issue_number}"
            self._request("PATCH", url, {"body": record.payload["body"]})
            return
        if record.stream is Stream.TASK_COMMENT:
            issue_number = int(record.payload["issue_number"])
            url = f"{self._base_url}/api/v1/repos/{record.target}/issues/{issue_number}/comments"
            marker = f"<!-- task-relay:idempotency_key={record.idempotency_key} -->"
            self._request("POST", url, {"body": f"{record.payload['body']}\n\n{marker}"})
            return
        if record.stream is Stream.TASK_LABEL_SYNC:
            issue_number = int(record.payload["issue_number"])
            url = f"{self._base_url}/api/v1/repos/{record.target}/issues/{issue_number}/labels"
            current_labels = self._request("GET", url)
            final_names = self._diff_labels(
                current=current_labels,
                managed=record.payload.get("managed_labels", sorted(MANAGED_LABELS)),
                desired=record.payload["desired_labels"],
            )
            self._request("PUT", url, {"labels": final_names})
            return
        raise ValueError(f"forgejo sink does not support stream={record.stream.value}")

    def _diff_labels(
        self,
        *,
        current: list[dict[str, Any]],
        managed: Iterable[str],
        desired: Iterable[str],
    ) -> list[str]:
        # WHY: manual Forgejo labels outside the managed allowlist must survive relay sync.
        managed_names = set(managed)
        keep_names = {str(label["name"]) for label in current if str(label["name"]) not in managed_names}
        return sorted(keep_names | set(desired))

    def _request(self, method: str, url: str, json: dict[str, Any] | None = None) -> Any:
        _ = (self._client, method, url, json)
        raise NotImplementedError("Phase 2 integration")
