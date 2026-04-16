from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_relay.clock import FrozenClock
from task_relay.config import Settings
from task_relay.db import queries
from task_relay.db.connection import connect
from task_relay.db.migrations import apply_schema
from task_relay.ingester.journal_ingester import JournalIngester
from task_relay.ingress.cli_source import build_cli_event, build_ingress_issue_event
from task_relay.journal.reader import JournalReader
from task_relay.journal.writer import JournalWriter
from task_relay.projection import LoggingSink
from task_relay.projection.rebuild import rebuild_for_task
from task_relay.projection.worker import ProjectionWorker
from task_relay.router.router import Router, RouterResult
from task_relay.runner.adapters.planner import PlannerAdapter
from task_relay.types import CanonicalEvent, Source, Stream, Task, TaskState

from tests.unit._fake_transports import FakeTransport
from tests.unit._test_helpers import insert_plan_row


@dataclass(frozen=True)
class Phase2Harness:
    conn: sqlite3.Connection
    settings: Settings
    writer: JournalWriter
    ingester: JournalIngester
    router: Router
    journal_dir: Path
    log_dir: Path
    worktree_root: Path
    git_repo: Path
    base_time: datetime


def test_phase2_e2e_full_lifecycle_reaches_done_and_drains_projection(tmp_path: Path, git_repo: Path) -> None:
    harness = _build_harness(tmp_path=tmp_path, git_repo=git_repo)
    try:
        task_id = _open_issue_to_planning(harness, source_issue_id="42")
        adapter_output = PlannerAdapter(
            FakeTransport([{"payload": _valid_plan_json()}]),
            sleep=_no_sleep,
        ).call(
            request_id="req-plan-full-lifecycle",
            payload={"task_id": task_id, "prompt": "produce a plan"},
        )
        assert adapter_output.ok is True
        insert_plan_row(
            harness.conn,
            task_id=task_id,
            plan_rev=1,
            plan_json=_valid_plan_json(),
            validator_score=int(adapter_output.payload["validator_score"]),
            validator_errors=int(adapter_output.payload["validator_errors"]),
            created_at=harness.base_time + timedelta(minutes=1),
        )

        _append_and_route(
            harness,
            _internal_event(
                event_id="evt-plan-ready-full-lifecycle",
                event_type="internal.plan_ready",
                task_id=task_id,
                received_at=harness.base_time + timedelta(minutes=1),
                payload={
                    "plan_rev": 1,
                    "plan_json": _valid_plan_json(),
                    "validator_score": int(adapter_output.payload["validator_score"]),
                    "validator_errors": int(adapter_output.payload["validator_errors"]),
                    "critical": False,
                },
            ),
        )

        worktree_path = harness.worktree_root / harness.git_repo.name / task_id
        worktree_path.mkdir(parents=True, exist_ok=True)
        feature_branch = f"task-relay/{task_id[:8]}"
        dispatch_result = _append_and_route(
            harness,
            _internal_event(
                event_id="evt-dispatch-full-lifecycle",
                event_type="internal.dispatch_attempt",
                task_id=task_id,
                received_at=harness.base_time + timedelta(minutes=2),
                payload={
                    "lease_acquired": True,
                    "lease_branch": "main",
                    "feature_branch": feature_branch,
                    "worktree_path": str(worktree_path),
                },
            ),
        )
        assert dispatch_result.to_state is TaskState.IMPLEMENTING

        implementing_task = _require_task(harness.conn, task_id)
        assert implementing_task.lease_branch == "main"
        assert implementing_task.feature_branch == feature_branch
        assert implementing_task.worktree_path == str(worktree_path)

        _append_and_route(
            harness,
            _internal_event(
                event_id="evt-executor-finished-full-lifecycle",
                event_type="internal.executor_finished",
                task_id=task_id,
                received_at=harness.base_time + timedelta(minutes=3),
                payload={
                    "exit_code": 0,
                    "changed_files": [],
                    "out_of_scope_files": [],
                },
            ),
        )
        review_result = _append_and_route(
            harness,
            _internal_event(
                event_id="evt-reviewer-pass-full-lifecycle",
                event_type="internal.reviewer_pass",
                task_id=task_id,
                received_at=harness.base_time + timedelta(minutes=4),
                payload={
                    "decision": "pass",
                    "unchecked": 0,
                    "manual_gate_required": False,
                },
            ),
        )

        done_task = _require_task(harness.conn, task_id)
        snapshot_rows = harness.conn.execute(
            """
            SELECT state_rev
            FROM projection_outbox
            WHERE task_id = ? AND stream = ?
            ORDER BY outbox_id
            """,
            (task_id, Stream.TASK_SNAPSHOT.value),
        ).fetchall()
        total_outbox_rows = harness.conn.execute(
            "SELECT COUNT(*) AS count FROM projection_outbox WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert review_result.to_state is TaskState.DONE
        assert done_task.state is TaskState.DONE
        assert [int(row["state_rev"]) for row in snapshot_rows] == [1, 2, 3, 4, 5]
        assert total_outbox_rows is not None
        assert int(total_outbox_rows["count"]) == 6

        snapshot_sink = LoggingSink()
        label_sink = LoggingSink()
        alert_sink = LoggingSink()
        comment_sink = LoggingSink()
        projection_worker = ProjectionWorker(
            harness.conn,
            sinks={
                Stream.TASK_SNAPSHOT: snapshot_sink,
                Stream.TASK_LABEL_SYNC: label_sink,
                Stream.DISCORD_ALERT: alert_sink,
                Stream.TASK_COMMENT: comment_sink,
            },
            settings=harness.settings,
            worker_id="test-projection-worker",
            clock=FrozenClock(harness.base_time + timedelta(hours=1)),
        )

        drained = 0
        while True:
            stepped = projection_worker.step()
            if stepped == 0:
                break
            drained += stepped

        cursor_row = harness.conn.execute(
            """
            SELECT last_sent_state_rev
            FROM projection_cursors
            WHERE task_id = ? AND stream = ? AND target = ?
            """,
            (task_id, Stream.TASK_SNAPSHOT.value, "42"),
        ).fetchone()
        assert drained == 6
        assert len(snapshot_sink.records) == 5
        assert len(label_sink.records) == 1
        assert len(alert_sink.records) == 0
        assert len(comment_sink.records) == 0
        assert cursor_row is not None
        assert int(cursor_row["last_sent_state_rev"]) > 0
    finally:
        harness.writer.close()
        harness.conn.close()


def test_phase2_e2e_planning_timeout_routes_to_human_review_required(tmp_path: Path, git_repo: Path) -> None:
    harness = _build_harness(tmp_path=tmp_path, git_repo=git_repo)
    try:
        task_id = _open_issue_to_planning(harness, source_issue_id="77")

        timeout_result = _append_and_route(
            harness,
            _internal_event(
                event_id="evt-planner-timeout-phase2",
                event_type="internal.planner_timeout",
                task_id=task_id,
                received_at=harness.base_time + timedelta(minutes=1),
                payload={"failure_code": "timeout"},
            ),
        )

        alert_rows = harness.conn.execute(
            """
            SELECT stream, target, payload_json
            FROM projection_outbox
            WHERE origin_event_id = ?
            ORDER BY outbox_id
            """,
            ("evt-planner-timeout-phase2",),
        ).fetchall()
        timed_out_task = _require_task(harness.conn, task_id)
        assert timeout_result.to_state is TaskState.HUMAN_REVIEW_REQUIRED
        assert timed_out_task.state is TaskState.HUMAN_REVIEW_REQUIRED
        assert [row["stream"] for row in alert_rows] == [
            Stream.DISCORD_ALERT.value,
            Stream.TASK_LABEL_SYNC.value,
        ]
        assert alert_rows[0]["target"] == "admin_user_ids"
        assert json.loads(str(alert_rows[0]["payload_json"]))["kind"] == "human_review_required"
    finally:
        harness.writer.close()
        harness.conn.close()


def test_phase2_e2e_cancel_mid_flight_updates_labels(tmp_path: Path, git_repo: Path) -> None:
    harness = _build_harness(tmp_path=tmp_path, git_repo=git_repo)
    try:
        task_id = _drive_task_to_implementing(harness, source_issue_id="88")

        cancel_result = _append_and_route(
            harness,
            build_cli_event(
                event_type="/cancel",
                task_id=task_id,
                actor="alice",
                clock=FrozenClock(harness.base_time + timedelta(minutes=3)),
            ),
        )

        cancelled_task = _require_task(harness.conn, task_id)
        label_row = harness.conn.execute(
            """
            SELECT payload_json
            FROM projection_outbox
            WHERE origin_event_id = ? AND stream = ?
            """,
            (cancel_result.event_id, Stream.TASK_LABEL_SYNC.value),
        ).fetchone()
        assert cancel_result.to_state is TaskState.CANCELLED
        assert cancelled_task.state is TaskState.CANCELLED
        assert label_row is not None
        assert "cancelled" in json.loads(str(label_row["payload_json"]))["desired_labels"]
    finally:
        harness.writer.close()
        harness.conn.close()


def test_phase2_e2e_projection_rebuild_round_trip(tmp_path: Path, git_repo: Path) -> None:
    harness = _build_harness(tmp_path=tmp_path, git_repo=git_repo)
    try:
        task_id = _drive_task_to_done(harness, source_issue_id="99")

        existing_row_count = harness.conn.execute(
            "SELECT COUNT(*) AS count FROM projection_outbox WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        rebuild_first = rebuild_for_task(
            harness.conn,
            task_id,
            clock=FrozenClock(harness.base_time + timedelta(hours=1)),
        )
        rebuild_second = rebuild_for_task(
            harness.conn,
            task_id,
            clock=FrozenClock(harness.base_time + timedelta(hours=1)),
        )
        forced = rebuild_for_task(
            harness.conn,
            task_id,
            force=True,
            clock=FrozenClock(harness.base_time + timedelta(hours=2)),
        )
        final_row_count = harness.conn.execute(
            "SELECT COUNT(*) AS count FROM projection_outbox WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert existing_row_count is not None
        assert int(existing_row_count["count"]) == 6
        assert rebuild_first == 1
        assert rebuild_second == 0
        assert forced == 2
        assert final_row_count is not None
        assert int(final_row_count["count"]) == 2
    finally:
        harness.writer.close()
        harness.conn.close()


def _build_harness(*, tmp_path: Path, git_repo: Path) -> Phase2Harness:
    base_time = datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)
    sqlite_path = tmp_path / "state.sqlite"
    journal_dir = tmp_path / "journal"
    log_dir = tmp_path / "logs"
    worktree_root = tmp_path / "worktrees"
    log_dir.mkdir(parents=True, exist_ok=True)
    worktree_root.mkdir(parents=True, exist_ok=True)
    conn = connect(sqlite_path)
    apply_schema(conn)
    settings = Settings(
        sqlite_path=sqlite_path,
        journal_dir=journal_dir,
        log_dir=log_dir,
        executor_workspace_root=worktree_root,
        forgejo_base_url="http://forgejo.local",
    )
    writer = JournalWriter(journal_dir, FrozenClock(base_time))
    ingester = JournalIngester(
        conn_factory=_conn_factory(sqlite_path),
        journal_reader=JournalReader(journal_dir),
        clock=FrozenClock(base_time + timedelta(seconds=1)),
    )
    router = Router(settings, clock=FrozenClock(base_time))
    return Phase2Harness(
        conn=conn,
        settings=settings,
        writer=writer,
        ingester=ingester,
        router=router,
        journal_dir=journal_dir,
        log_dir=log_dir,
        worktree_root=worktree_root,
        git_repo=git_repo,
        base_time=base_time,
    )


def _open_issue_to_planning(harness: Phase2Harness, *, source_issue_id: str) -> str:
    opened_result = _append_and_route(
        harness,
        build_ingress_issue_event(
            source=Source.FORGEJO,
            event_type="issues.opened",
            delivery_id=f"delivery-opened-{source_issue_id}",
            payload={
                "source_issue_id": source_issue_id,
                "sender_login": "alice",
                "base_branch": "main",
            },
            clock=FrozenClock(harness.base_time),
        ),
    )
    assert opened_result.to_state is TaskState.PLANNING
    assert opened_result.task_id is not None
    return opened_result.task_id


def _drive_task_to_implementing(harness: Phase2Harness, *, source_issue_id: str) -> str:
    task_id = _open_issue_to_planning(harness, source_issue_id=source_issue_id)
    planner_result = PlannerAdapter(
        FakeTransport([{"payload": _valid_plan_json()}]),
        sleep=_no_sleep,
    ).call(
        request_id=f"req-plan-{source_issue_id}",
        payload={"task_id": task_id, "prompt": "produce a plan"},
    )
    assert planner_result.ok is True
    insert_plan_row(
        harness.conn,
        task_id=task_id,
        plan_rev=1,
        plan_json=_valid_plan_json(),
        validator_score=int(planner_result.payload["validator_score"]),
        validator_errors=int(planner_result.payload["validator_errors"]),
        created_at=harness.base_time + timedelta(minutes=1),
    )
    _append_and_route(
        harness,
        _internal_event(
            event_id=f"evt-plan-ready-{source_issue_id}",
            event_type="internal.plan_ready",
            task_id=task_id,
            received_at=harness.base_time + timedelta(minutes=1),
            payload={
                "plan_rev": 1,
                "plan_json": _valid_plan_json(),
                "validator_score": int(planner_result.payload["validator_score"]),
                "validator_errors": int(planner_result.payload["validator_errors"]),
                "critical": False,
            },
        ),
    )
    worktree_path = harness.worktree_root / harness.git_repo.name / task_id
    worktree_path.mkdir(parents=True, exist_ok=True)
    dispatch_result = _append_and_route(
        harness,
        _internal_event(
            event_id=f"evt-dispatch-{source_issue_id}",
            event_type="internal.dispatch_attempt",
            task_id=task_id,
            received_at=harness.base_time + timedelta(minutes=2),
            payload={
                "lease_acquired": True,
                "lease_branch": "main",
                "feature_branch": f"task-relay/{task_id[:8]}",
                "worktree_path": str(worktree_path),
            },
        ),
    )
    assert dispatch_result.to_state is TaskState.IMPLEMENTING
    return task_id


def _drive_task_to_done(harness: Phase2Harness, *, source_issue_id: str) -> str:
    task_id = _drive_task_to_implementing(harness, source_issue_id=source_issue_id)
    _append_and_route(
        harness,
        _internal_event(
            event_id=f"evt-executor-finished-{source_issue_id}",
            event_type="internal.executor_finished",
            task_id=task_id,
            received_at=harness.base_time + timedelta(minutes=3),
            payload={
                "exit_code": 0,
                "changed_files": [],
                "out_of_scope_files": [],
            },
        ),
    )
    done_result = _append_and_route(
        harness,
        _internal_event(
            event_id=f"evt-reviewer-pass-{source_issue_id}",
            event_type="internal.reviewer_pass",
            task_id=task_id,
            received_at=harness.base_time + timedelta(minutes=4),
            payload={
                "decision": "pass",
                "unchecked": 0,
                "manual_gate_required": False,
            },
        ),
    )
    assert done_result.to_state is TaskState.DONE
    return task_id


def _append_and_route(harness: Phase2Harness, event: CanonicalEvent) -> RouterResult:
    harness.writer.append(event)
    assert harness.ingester.step() == 1
    inbox_event = queries.fetch_next_unprocessed(harness.conn)
    assert inbox_event is not None
    return harness.router.run_once(harness.conn, inbox_event)


def _internal_event(
    *,
    event_id: str,
    event_type: str,
    task_id: str,
    received_at: datetime,
    payload: dict[str, object] | None = None,
) -> CanonicalEvent:
    event_payload: dict[str, object] = {"task_id": task_id}
    if payload is not None:
        event_payload.update(payload)
    return CanonicalEvent(
        event_id=event_id,
        source=Source.INTERNAL,
        delivery_id=f"delivery-{event_id}",
        event_type=event_type,
        payload=event_payload,
        received_at=received_at,
        request_id=f"request-{event_id}",
    )


def _require_task(conn: sqlite3.Connection, task_id: str) -> Task:
    task = queries.get_task(conn, task_id)
    assert task is not None
    return task


def _valid_plan_json() -> dict[str, object]:
    return {
        "goal": "Implement the requested lifecycle",
        "sub_tasks": ["Plan the work", "Apply the implementation", "Review the outcome"],
        "allowed_files": ["src/task_relay/**/*.py"],
        "auto_allowed_patterns": ["tests/**/*.py"],
        "acceptance_criteria": ["The requested change reaches done without manual intervention"],
        "forbidden_changes": ["Do not modify database queries in this test"],
        "risk_notes": ["Projection outbox idempotency depends on the final state revision"],
    }


def _conn_factory(sqlite_path: Path) -> Callable[[], sqlite3.Connection]:
    def factory() -> sqlite3.Connection:
        conn = connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    return factory


def _no_sleep(_: float) -> None:
    return None
