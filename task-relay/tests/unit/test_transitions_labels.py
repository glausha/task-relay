from __future__ import annotations

import json
from datetime import datetime, timezone

from task_relay.config import Settings
from task_relay.db import queries
from task_relay.projection.labels import MANAGED_LABELS
from task_relay.router.router import Router
from task_relay.types import InboxEvent, Source, Stream, TaskState

from tests.unit._test_helpers import seed_task


def _event(*, event_id: str, event_type: str, payload: dict[str, object], source: Source = Source.INTERNAL) -> InboxEvent:
    return InboxEvent(
        event_id=event_id,
        source=source,
        delivery_id=f"delivery-{event_id}",
        event_type=event_type,
        payload=payload,
        journal_offset=0,
        received_at=datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc),
    )


def test_cancelled_transition_emits_allowlist_labels_payload(sqlite_conn) -> None:
    router = Router(Settings())
    task = seed_task(
        sqlite_conn,
        task_id="task-labels-1",
        created_at=datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc),
        state=TaskState.NEEDS_FIX,
        critical=True,
    )
    event = _event(
        event_id="evt-cancelled-labels",
        event_type="/cancel",
        payload={"task_id": task.task_id},
        source=Source.CLI,
    )
    queries.insert_event(sqlite_conn, event)

    result = router.run_once(sqlite_conn, event)
    row = sqlite_conn.execute(
        """
        SELECT payload_json
        FROM projection_outbox
        WHERE origin_event_id = ? AND stream = ?
        """,
        (event.event_id, Stream.TASK_LABEL_SYNC.value),
    ).fetchone()

    assert result.to_state is TaskState.CANCELLED
    assert row is not None
    payload = json.loads(str(row["payload_json"]))
    assert payload["desired_labels"] == ["cancelled", "critical"]
    assert set(payload["managed_labels"]) == MANAGED_LABELS
