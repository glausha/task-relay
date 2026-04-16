from __future__ import annotations

import json
from datetime import datetime, timezone

from task_relay.clock import FrozenClock
from task_relay.db.queries import insert_outbox, mark_outbox_sent
from task_relay.projection.labels import MANAGED_LABELS
from task_relay.projection.rebuild import rebuild_for_task
from task_relay.types import Stream, TaskState

from tests.unit._test_helpers import insert_plan_row, seed_task


def test_rebuild_creates_snapshot_and_label_sync_rows(sqlite_conn) -> None:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    task = seed_task(
        sqlite_conn,
        task_id="task-rebuild-1",
        source_issue_id="42",
        created_at=now,
        state=TaskState.HUMAN_REVIEW_REQUIRED,
        state_rev=3,
        critical=True,
    )
    insert_plan_row(
        sqlite_conn,
        task_id=task.task_id,
        plan_rev=2,
        plan_json={"allowed_files": ["src/task_relay/projection/rebuild.py"]},
        created_at=now,
    )

    count = rebuild_for_task(sqlite_conn, task.task_id, clock=FrozenClock(now))

    rows = sqlite_conn.execute(
        """
        SELECT stream, target, payload_json
        FROM projection_outbox
        WHERE task_id = ?
        ORDER BY stream
        """,
        (task.task_id,),
    ).fetchall()

    assert count == 2
    assert [row["stream"] for row in rows] == ["task_label_sync", "task_snapshot"]
    assert all(row["target"] == "42" for row in rows)

    label_payload = json.loads(str(rows[0]["payload_json"]))
    snapshot_payload = json.loads(str(rows[1]["payload_json"]))
    assert label_payload["desired_labels"] == ["critical", "human_review_required"]
    assert set(label_payload["managed_labels"]) == MANAGED_LABELS
    assert snapshot_payload == {
        "state": "human_review_required",
        "state_rev": 3,
        "plan_rev": 2,
        "critical": True,
        "task_url": "http://localhost:3000/issues/42",
    }


def test_rebuild_is_idempotent_when_same_rows_already_exist(sqlite_conn) -> None:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    task = seed_task(
        sqlite_conn,
        task_id="task-rebuild-2",
        created_at=now,
        state=TaskState.DONE,
        state_rev=5,
    )
    insert_plan_row(
        sqlite_conn,
        task_id=task.task_id,
        plan_rev=1,
        plan_json={"acceptance_criteria": ["done"]},
        created_at=now,
    )

    first = rebuild_for_task(sqlite_conn, task.task_id, clock=FrozenClock(now))
    second = rebuild_for_task(sqlite_conn, task.task_id, clock=FrozenClock(now))

    row_count = sqlite_conn.execute(
        "SELECT COUNT(*) FROM projection_outbox WHERE task_id = ?",
        (task.task_id,),
    ).fetchone()[0]
    assert first == 2
    assert second == 0
    assert row_count == 2


def test_force_rebuild_deletes_pending_rows_then_reinserts(sqlite_conn) -> None:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    task = seed_task(
        sqlite_conn,
        task_id="task-rebuild-3",
        created_at=now,
        state=TaskState.CANCELLED,
        state_rev=4,
        critical=False,
    )

    initial = rebuild_for_task(sqlite_conn, task.task_id, clock=FrozenClock(now))
    forced = rebuild_for_task(sqlite_conn, task.task_id, force=True, clock=FrozenClock(now))

    rows = sqlite_conn.execute(
        """
        SELECT stream, sent_at
        FROM projection_outbox
        WHERE task_id = ?
        ORDER BY outbox_id
        """,
        (task.task_id,),
    ).fetchall()

    assert initial == 2
    assert forced == 2
    assert len(rows) == 2
    assert {row["stream"] for row in rows} == {Stream.TASK_SNAPSHOT.value, Stream.TASK_LABEL_SYNC.value}
    assert all(row["sent_at"] is None for row in rows)


def test_force_rebuild_keeps_sent_task_comment_rows(sqlite_conn) -> None:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    task = seed_task(
        sqlite_conn,
        task_id="task-rebuild-4",
        source_issue_id="99",
        created_at=now,
        state=TaskState.DONE,
        state_rev=7,
    )

    rebuild_for_task(sqlite_conn, task.task_id, clock=FrozenClock(now))
    comment_outbox_id = insert_outbox(
        sqlite_conn,
        task_id=task.task_id,
        stream=Stream.TASK_COMMENT,
        target="99",
        origin_event_id="evt-comment-sent",
        payload_json=json.dumps({"body": "already sent"}, separators=(",", ":")),
        state_rev=task.state_rev,
        idempotency_key="comment-sent-1",
        next_attempt_at=now.isoformat(),
    )
    mark_outbox_sent(sqlite_conn, comment_outbox_id, now)

    forced = rebuild_for_task(sqlite_conn, task.task_id, force=True, clock=FrozenClock(now))

    rows = sqlite_conn.execute(
        """
        SELECT stream, sent_at
        FROM projection_outbox
        WHERE task_id = ?
        ORDER BY outbox_id
        """,
        (task.task_id,),
    ).fetchall()

    assert forced == 2
    assert len(rows) == 3
    assert sum(1 for row in rows if row["stream"] == Stream.TASK_COMMENT.value) == 1
    assert sum(1 for row in rows if row["sent_at"] is not None) == 1
