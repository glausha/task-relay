from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import httpx

from task_relay.db.connection import connect
from task_relay.db.migrations import apply_schema
from task_relay.projection.forgejo_sink import ForgejoSink
from task_relay.types import OutboxRecord, Severity, Stream, SystemEventType


def test_send_task_snapshot_patches_frontmatter() -> None:
    calls: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True})

    sink = _sink(handler)

    sink.send(
        _record(
            stream=Stream.TASK_SNAPSHOT,
            target="42",
            payload={
                "source_issue_id": "42",
                "state": "planning",
                "state_rev": 3,
                "plan_rev": 1,
                "critical": False,
                "task_url": "https://forgejo.local/issues/42",
            },
        )
    )

    assert calls == [
        (
            "PATCH",
            "/api/v1/repos/org/repo/issues/42",
            '{"body":"---\\nstate: planning\\nstate_rev: 3\\nplan_rev: 1\\ncritical: false\\ntask_url: https://forgejo.local/issues/42\\n---"}',
        )
    ]


def test_send_task_snapshot_checks_mirror_and_still_overwrites(tmp_path) -> None:
    calls: list[tuple[str, str, object]] = []
    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content.decode("utf-8")))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"body": "---\nstate: done\nstate_rev: 3\n---\n\nRemote body"},
            )
        return httpx.Response(200, json={"ok": True})

    sink = _sink(handler, conn=conn)

    try:
        sink.send(
            _record(
                stream=Stream.TASK_SNAPSHOT,
                target="42",
                payload={
                    "source_issue_id": "42",
                    "state": "planning",
                    "state_rev": 3,
                    "plan_rev": 1,
                    "critical": False,
                    "task_url": "https://forgejo.local/issues/42",
                },
            )
        )
    finally:
        row = conn.execute(
            """
            SELECT event_type, severity, payload_json
            FROM system_events
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        conn.close()

    assert calls == [
        ("GET", "/api/v1/repos/org/repo/issues/42", ""),
        (
            "PATCH",
            "/api/v1/repos/org/repo/issues/42",
            '{"body":"---\\nstate: planning\\nstate_rev: 3\\nplan_rev: 1\\ncritical: false\\ntask_url: https://forgejo.local/issues/42\\n---"}',
        ),
    ]
    assert row is not None
    assert row["event_type"] == SystemEventType.MIRROR_READONLY_VIOLATION_DETECTED.value
    assert row["severity"] == Severity.WARNING.value
    assert json.loads(str(row["payload_json"]))["changed_fields"] == ["critical", "plan_rev", "state", "task_url"]


def test_send_task_comment_posts_audit_body_with_marker() -> None:
    calls: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content.decode("utf-8")))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 1})

    sink = _sink(handler)

    sink.send(
        _record(
            stream=Stream.TASK_COMMENT,
            target="42",
            payload={"body": "Audit entry"},
            idempotency_key="idem-comment",
        )
    )

    assert calls == [
        ("GET", "/api/v1/repos/org/repo/issues/42/comments", ""),
        (
            "POST",
            "/api/v1/repos/org/repo/issues/42/comments",
            '{"body":"Audit entry\\n\\n<!-- task-relay:idempotency_key=idem-comment -->"}',
        ),
    ]


def test_send_task_label_sync_gets_repo_labels_then_puts_label_ids() -> None:
    calls: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content.decode("utf-8")))
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "name": "critical"},
                    {"id": 2, "name": "cancelled"},
                    {"id": 3, "name": "bug"},
                ],
            )
        return httpx.Response(200, json=[{"id": 2}, {"id": 3}])

    sink = _sink(handler)

    sink.send(
        _record(
            stream=Stream.TASK_LABEL_SYNC,
            target="42",
            payload={
                "desired_labels": ["cancelled"],
                "managed_labels": ["critical", "cancelled"],
                "current_labels": [{"name": "critical"}, {"name": "bug"}],
            },
        )
    )

    assert calls == [
        ("GET", "/api/v1/repos/org/repo/labels", ""),
        ("PUT", "/api/v1/repos/org/repo/issues/42/labels", '{"labels":[3,2]}'),
    ]


def test_send_task_comment_skips_post_when_marker_already_exists() -> None:
    calls: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json=[{"body": "previous\n\n<!-- task-relay:idempotency_key=idem-comment -->"}],
        )

    sink = _sink(handler)

    sink.send(
        _record(
            stream=Stream.TASK_COMMENT,
            target="42",
            payload={"body": "Audit entry"},
            idempotency_key="idem-comment",
        )
    )

    assert calls == [("GET", "/api/v1/repos/org/repo/issues/42/comments", "")]


def _sink(handler, conn: sqlite3.Connection | None = None) -> ForgejoSink:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(
        base_url="http://forgejo.local",
        headers={"Authorization": "token token"},
        timeout=30.0,
        transport=transport,
    )
    return ForgejoSink(
        base_url="http://forgejo.local",
        token="token",
        owner="org",
        repo="repo",
        conn=conn,
        client=client,
    )


def _record(
    *,
    stream: Stream,
    target: str,
    payload: dict[str, object],
    idempotency_key: str = "idem-1",
) -> OutboxRecord:
    now = datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)
    return OutboxRecord(
        outbox_id=1,
        task_id="task-1",
        stream=stream,
        target=target,
        origin_event_id="event-1",
        payload=payload,
        state_rev=1,
        idempotency_key=idempotency_key,
        attempt_count=0,
        next_attempt_at=now,
        sent_at=None,
    )
