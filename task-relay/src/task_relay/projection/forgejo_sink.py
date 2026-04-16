from __future__ import annotations

from collections.abc import Iterable, Mapping
import sqlite3
from typing import Any

import httpx
import yaml

from task_relay.projection.mirror_check import check_mirror_consistency
from task_relay.projection.labels import MANAGED_LABELS
from task_relay.types import OutboxRecord
from task_relay.types import Stream


class ForgejoSink:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        owner: str,
        repo: str,
        conn: sqlite3.Connection | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._client = client or httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"token {token}"},
            timeout=30.0,
        )
        self._conn = conn
        self._owner = owner
        self._repo = repo

    def send(self, record: OutboxRecord) -> None:
        issue_number = self._issue_number(record)
        if record.stream is Stream.TASK_SNAPSHOT:
            snapshot_body = self._snapshot_body(record.payload)
            if self._conn is not None:
                issue = self._request("GET", self._issue_path(issue_number))
                remote_body = str(issue.get("body", "")) if isinstance(issue, dict) else ""
                check_mirror_consistency(
                    self._conn,
                    task_id=record.task_id,
                    remote_body=remote_body,
                    expected_body=snapshot_body,
                )
            self._request(
                "PATCH",
                self._issue_path(issue_number),
                json={"body": snapshot_body},
            )
            return
        if record.stream is Stream.TASK_COMMENT:
            marker = f"<!-- task-relay:idempotency_key={record.idempotency_key} -->"
            comments = self._request("GET", self._comments_path(issue_number))
            if any(marker in str(comment.get("body", "")) for comment in self._as_items(comments)):
                return
            body = str(record.payload.get("body", "")).rstrip()
            self._request("POST", self._comments_path(issue_number), json={"body": f"{body}\n\n{marker}"})
            return
        if record.stream is Stream.TASK_LABEL_SYNC:
            current_labels = record.payload.get("current_labels")
            if not isinstance(current_labels, list):
                current_labels = self._request("GET", self._issue_labels_path(issue_number))
            final_names = self._diff_labels(
                current=current_labels,
                managed=record.payload.get("managed_labels", sorted(MANAGED_LABELS)),
                desired=record.payload["desired_labels"],
            )
            label_ids = self._lookup_label_ids(final_names)
            self._request("PUT", self._issue_labels_path(issue_number), json={"labels": label_ids})
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

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> Any:
        resp = self._client.request(method, path, json=json)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _lookup_label_ids(self, names: Iterable[str]) -> list[int]:
        wanted = set(names)
        if not wanted:
            return []
        labels = self._request("GET", self._repo_labels_path())
        id_by_name = {
            str(label["name"]): int(label["id"])
            for label in self._as_items(labels)
            if "name" in label and "id" in label
        }
        missing = sorted(wanted - set(id_by_name))
        if missing:
            raise ValueError(f"forgejo labels not found: {', '.join(missing)}")
        return [id_by_name[name] for name in sorted(wanted)]

    def _snapshot_body(self, payload: Mapping[str, Any]) -> str:
        frontmatter_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"body", "current_labels", "desired_labels", "issue_number", "managed_labels", "source_issue_id"}
        }
        frontmatter = yaml.safe_dump(
            frontmatter_payload,
            allow_unicode=True,
            sort_keys=False,
        ).strip()
        body = payload.get("body")
        if isinstance(body, str) and body.strip():
            return f"---\n{frontmatter}\n---\n\n{body.strip()}"
        return f"---\n{frontmatter}\n---"

    def _issue_number(self, record: OutboxRecord) -> int:
        value = record.payload.get("source_issue_id") or record.payload.get("issue_number") or record.target
        return int(str(value))

    def _issue_path(self, issue_number: int) -> str:
        return f"/api/v1/repos/{self._owner}/{self._repo}/issues/{issue_number}"

    def _comments_path(self, issue_number: int) -> str:
        return f"{self._issue_path(issue_number)}/comments"

    def _issue_labels_path(self, issue_number: int) -> str:
        return f"{self._issue_path(issue_number)}/labels"

    def _repo_labels_path(self) -> str:
        return f"/api/v1/repos/{self._owner}/{self._repo}/labels"

    def _as_items(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]
