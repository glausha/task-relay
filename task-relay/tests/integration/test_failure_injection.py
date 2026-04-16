from __future__ import annotations

import asyncio
import json
import os
import shlex
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from task_relay.clock import FrozenClock
from task_relay.config import Settings
from task_relay.db import queries
from task_relay.db.connection import connect
from task_relay.db.migrations import apply_schema
from task_relay.errors import FailureCode
from task_relay.ingester.journal_ingester import JournalIngester
from task_relay.ingress.cli_source import build_cli_event, build_ingress_issue_event
from task_relay.ingress.discord_gateway import DiscordIngress
from task_relay.journal.reader import JournalReader
from task_relay.journal.writer import JournalWriter
from task_relay.projection.forgejo_sink import ForgejoSink
from task_relay.projection.worker import ProjectionWorker
from task_relay.router.router import Router
from task_relay.runner.tool_runner import ToolRunner
from task_relay.types import CanonicalEvent, InboxEvent, JournalPosition, OutboxRecord, Source, Stage, Stream, TaskState

from tests.unit._test_helpers import seed_task


class _ControlledJournalWriter:
    def __init__(self) -> None:
        self.appended_event_ids: list[str] = []

    def append(self, event: CanonicalEvent) -> JournalPosition:
        self.appended_event_ids.append(event.event_id)
        return JournalPosition(file="journal-20260416.zst", offset=len(self.appended_event_ids))


class _BlockedDiscordIngress(DiscordIngress):
    def __init__(self, journal_writer: _ControlledJournalWriter, release: asyncio.Event, **kwargs: object) -> None:
        super().__init__(journal_writer, **kwargs)
        self._release = release

    async def _writer_loop(self) -> None:
        await self._release.wait()
        await super()._writer_loop()


class _CrashAfterRemoteSendSink:
    def __init__(self, delegate: ForgejoSink) -> None:
        self._delegate = delegate

    def send(self, record: OutboxRecord) -> None:
        self._delegate.send(record)
        raise RuntimeError("crash after remote success")


class _AlwaysFailingSink:
    def send(self, record: OutboxRecord) -> None:
        _ = record
        raise RuntimeError("boom")


async def test_failure_injection_ingress_discord_1500ms_timeout_is_not_accepted() -> None:
    release = asyncio.Event()
    writer = _ControlledJournalWriter()
    ingress = _BlockedDiscordIngress(writer, release, ack_deadline_ms=1500, queue_capacity=2)
    first_event = build_cli_event(
        event_type="/approve",
        task_id="task-blocking",
        actor="42",
        clock=FrozenClock(datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)),
    )
    await ingress.start()
    first_result: asyncio.Future[JournalPosition] = asyncio.get_running_loop().create_future()
    ingress._queue.put_nowait((first_event, first_result))
    await asyncio.sleep(0.1)

    message, request_id = await ingress.handle_slash_command(
        command="/approve",
        user_id=42,
        task_id="task-42",
        extra_payload=None,
        admin_user_ids=[],
        get_requested_by=lambda _task_id: "discord:42",
    )

    assert request_id is not None
    assert message == f"Task relay did not confirm durable acceptance in time. request_id={request_id}"

    release.set()
    async with asyncio.timeout(5):
        await first_result
        await ingress.stop()

    assert writer.appended_event_ids == [first_event.event_id]


def test_failure_injection_router_transaction_outbox_insert_before_commit_crash_rolls_back(
    sqlite_conn: sqlite3.Connection,
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)
    seed_task(
        sqlite_conn,
        task_id="task-router-crash",
        created_at=now,
        state=TaskState.PLANNING,
        state_rev=4,
        requested_by="discord:42",
        notification_target="42",
    )
    router = Router(Settings(), clock=FrozenClock(now))
    event = build_cli_event(
        event_type="/cancel",
        task_id="task-router-crash",
        actor="42",
        clock=FrozenClock(now + timedelta(seconds=1)),
    )
    inbox_event = _insert_inbox_event(sqlite_conn, event)
    original_mark_processed = queries.mark_processed

    def crash_after_mark_processed(conn: sqlite3.Connection, event_id: str, processed_at: datetime) -> None:
        original_mark_processed(conn, event_id, processed_at)
        raise RuntimeError("commit boundary crash")

    monkeypatch.setattr(queries, "mark_processed", crash_after_mark_processed)

    try:
        router.run_once(sqlite_conn, inbox_event)
    except RuntimeError as exc:
        assert str(exc) == "commit boundary crash"
    else:
        raise AssertionError("expected router transaction crash")

    task = queries.get_task(sqlite_conn, "task-router-crash")
    outbox_count = sqlite_conn.execute("SELECT COUNT(*) AS count FROM projection_outbox").fetchone()
    inbox_row = sqlite_conn.execute(
        "SELECT processed_at FROM event_inbox WHERE event_id = ?",
        (event.event_id,),
    ).fetchone()

    assert task is not None
    assert task.state is TaskState.PLANNING
    assert task.state_rev == 4
    assert outbox_count is not None
    assert int(outbox_count["count"]) == 0
    assert inbox_row is not None
    assert inbox_row["processed_at"] is None


def test_failure_injection_projection_remote_success_then_crash_before_sent_at_dedups_on_retry(
    sqlite_conn: sqlite3.Connection,
) -> None:
    now = datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)
    seed_task(
        sqlite_conn,
        task_id="task-projection-crash",
        created_at=now,
        state=TaskState.DONE,
        state_rev=2,
        source_issue_id="42",
    )
    outbox_id = queries.insert_outbox(
        sqlite_conn,
        task_id="task-projection-crash",
        stream=Stream.TASK_COMMENT,
        target="42",
        origin_event_id="evt-projection-crash",
        payload_json=json.dumps({"body": "Audit entry"}, separators=(",", ":"), ensure_ascii=False),
        state_rev=2,
        idempotency_key="idem-comment-crash",
        next_attempt_at=now.isoformat(),
    )
    calls: list[tuple[str, str]] = []
    remote_comments: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=[{"body": body} for body in remote_comments])
        payload = json.loads(request.content.decode("utf-8"))
        remote_comments.append(str(payload["body"]))
        return httpx.Response(201, json={"id": len(remote_comments)})

    client = httpx.Client(
        base_url="http://forgejo.local",
        headers={"Authorization": "token token"},
        transport=httpx.MockTransport(handler),
        timeout=30.0,
    )
    base_sink = ForgejoSink(
        base_url="http://forgejo.local",
        token="token",
        owner="org",
        repo="repo",
        client=client,
    )
    worker1 = ProjectionWorker(
        sqlite_conn,
        sinks={Stream.TASK_COMMENT: _CrashAfterRemoteSendSink(base_sink)},
        settings=Settings(),
        worker_id="projection-1",
        clock=FrozenClock(now),
    )
    worker2 = ProjectionWorker(
        sqlite_conn,
        sinks={Stream.TASK_COMMENT: base_sink},
        settings=Settings(),
        worker_id="projection-1",
        clock=FrozenClock(now + timedelta(seconds=61)),
    )

    assert worker1.step() == 1
    first_row = sqlite_conn.execute(
        "SELECT attempt_count, sent_at FROM projection_outbox WHERE outbox_id = ?",
        (outbox_id,),
    ).fetchone()
    assert first_row is not None
    assert first_row["attempt_count"] == 1
    assert first_row["sent_at"] is None

    assert worker2.step() == 1
    final_row = sqlite_conn.execute(
        "SELECT attempt_count, sent_at FROM projection_outbox WHERE outbox_id = ?",
        (outbox_id,),
    ).fetchone()
    assert final_row is not None
    assert final_row["attempt_count"] == 1
    assert final_row["sent_at"] is not None
    assert len(remote_comments) == 1
    assert calls == [
        ("GET", "/api/v1/repos/org/repo/issues/42/comments"),
        ("POST", "/api/v1/repos/org/repo/issues/42/comments"),
        ("GET", "/api/v1/repos/org/repo/issues/42/comments"),
    ]


def test_failure_injection_projection_retry_max_attempts_records_system_event(
    sqlite_conn: sqlite3.Connection,
) -> None:
    base_time = datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)
    seed_task(
        sqlite_conn,
        task_id="task-projection-retry-limit",
        created_at=base_time,
        state=TaskState.CANCELLED,
        state_rev=3,
        source_issue_id="42",
    )
    outbox_id = queries.insert_outbox(
        sqlite_conn,
        task_id="task-projection-retry-limit",
        stream=Stream.TASK_COMMENT,
        target="42",
        origin_event_id="evt-retry-limit",
        payload_json=json.dumps({"body": "Audit entry"}, separators=(",", ":"), ensure_ascii=False),
        state_rev=3,
        idempotency_key="idem-retry-limit",
        next_attempt_at=base_time.isoformat(),
    )
    settings = Settings(
        projection_retry_initial_seconds=1,
        projection_retry_cap_seconds=1,
        projection_retry_max_attempts=3,
    )

    for offset_seconds in (0, 2, 4):
        worker = ProjectionWorker(
            sqlite_conn,
            sinks={Stream.TASK_COMMENT: _AlwaysFailingSink()},
            settings=settings,
            worker_id="projection-retry-limit",
            clock=FrozenClock(base_time + timedelta(seconds=offset_seconds)),
        )
        assert worker.step() == 1

    row = sqlite_conn.execute(
        """
        SELECT event_type, severity, payload_json
        FROM system_events
        WHERE task_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        ("task-projection-retry-limit",),
    ).fetchone()
    outbox_row = sqlite_conn.execute(
        "SELECT attempt_count, sent_at FROM projection_outbox WHERE outbox_id = ?",
        (outbox_id,),
    ).fetchone()
    task = queries.get_task(sqlite_conn, "task-projection-retry-limit")

    assert row is not None
    assert row["event_type"] == "internal.infra_fatal"
    assert row["severity"] == "error"
    assert json.loads(str(row["payload_json"]))["attempt_count"] == 3
    assert outbox_row is not None
    assert outbox_row["attempt_count"] == 3
    assert outbox_row["sent_at"] is None
    assert task is not None
    assert task.state is TaskState.CANCELLED


def test_failure_injection_disaster_recovery_missing_journal_ingester_state_replays_with_inbox_dedup(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "state.sqlite"
    journal_dir = tmp_path / "journal"
    conn = connect(sqlite_path)
    apply_schema(conn)
    conn.row_factory = sqlite3.Row
    writer = JournalWriter(journal_dir, FrozenClock(datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)))
    reader = JournalReader(journal_dir)
    ingester = JournalIngester(
        conn_factory=_sqlite_conn_factory(sqlite_path),
        journal_reader=reader,
        clock=FrozenClock(datetime(2026, 4, 16, 0, 1, tzinfo=timezone.utc)),
    )
    try:
        writer.append(
            build_ingress_issue_event(
                source=Source.CLI,
                event_type="issues.opened",
                delivery_id="delivery-restore-1",
                payload={"source_issue_id": "41", "actor": "alice"},
                clock=FrozenClock(datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)),
            )
        )
        writer.append(
            build_ingress_issue_event(
                source=Source.CLI,
                event_type="issues.opened",
                delivery_id="delivery-restore-2",
                payload={"source_issue_id": "42", "actor": "alice"},
                clock=FrozenClock(datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)),
            )
        )

        assert ingester.step() == 2
        conn.execute("DELETE FROM journal_ingester_state WHERE singleton_id = 1")
        replayed = ingester.step()
        inbox_count_row = conn.execute("SELECT COUNT(*) AS count FROM event_inbox").fetchone()
        state_row = conn.execute(
            "SELECT last_file, last_offset FROM journal_ingester_state WHERE singleton_id = 1"
        ).fetchone()

        assert replayed == 2
        assert inbox_count_row is not None
        assert int(inbox_count_row["count"]) == 2
        assert state_row is not None
        assert state_row["last_file"] is not None
        assert int(state_row["last_offset"]) > 0
    finally:
        writer.close()
        conn.close()


def test_failure_injection_disaster_recovery_offsite_journal_lag_exits_non_zero(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    task_relay_stub = bin_dir / "task-relay"
    task_relay_stub.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$1\" in\n"
        "  db-check|journal-replay|reconcile|health-check)\n"
        "    exit 0\n"
        "    ;;\n"
        "  *)\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    task_relay_stub.chmod(0o755)
    script_path = Path(__file__).resolve().parents[2] / "deploy" / "restore-drill.sh"
    sqlite_path = tmp_path / "state.sqlite"
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    command = (
        f"{shlex.quote(str(script_path))} --max-lag-seconds 60 "
        f"{shlex.quote(str(sqlite_path))} {shlex.quote(str(journal_dir))}"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TASK_RELAY_JOURNAL_OFFSITE_LAG_SECONDS": "61",
    }

    result = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(script_path.parent.parent),
        check=False,
    )

    assert result.returncode != 0
    assert "journal_offsite_lag_seconds=61 exceeds 60" in result.stderr


def test_failure_injection_toolrunner_cancelled_mid_flight_records_subprocess_failure(
    tmp_path: Path,
    git_repo: Path,
    monkeypatch,
) -> None:
    sqlite_path = tmp_path / "state.sqlite"
    conn = connect(sqlite_path)
    apply_schema(conn)
    conn.row_factory = sqlite3.Row
    now = datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)
    seed_task(
        conn,
        task_id="task-cancel-mid-flight",
        created_at=now,
        state=TaskState.IMPLEMENTING,
        lease_branch="main",
        source_issue_id="42",
    )
    conn.commit()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude_stub = bin_dir / "claude"
    claude_stub.write_text(
        "#!/usr/bin/env bash\n"
        "trap 'exit 143' TERM\n"
        "while kill -0 \"$PPID\" 2>/dev/null; do\n"
        "  sleep 0.1\n"
        "done\n"
        "exit 143\n",
        encoding="utf-8",
    )
    claude_stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    settings = Settings(
        sqlite_path=sqlite_path,
        journal_dir=tmp_path / "journal",
        log_dir=tmp_path / "logs",
        executor_workspace_root=tmp_path / "worktrees",
        subprocess_sigterm_grace_seconds=1,
        executor_timeout=30,
    )
    writer = JournalWriter(settings.journal_dir, FrozenClock(now))
    runner = ToolRunner(
        "task-cancel-mid-flight",
        _sqlite_conn_factory(sqlite_path),
        writer,
        redis_client=object(),
        settings=settings,
        repo_root=git_repo,
        clock=FrozenClock(now),
    )
    runner.setup_worktree(lease_branch="main")

    def cancel_task() -> None:
        time.sleep(1.2)
        cancel_conn = connect(sqlite_path)
        cancel_conn.row_factory = sqlite3.Row
        try:
            queries.update_task_state(
                cancel_conn,
                task_id="task-cancel-mid-flight",
                new_state=TaskState.CANCELLED,
                new_state_rev=1,
                updated_at=now + timedelta(seconds=2),
            )
            cancel_conn.commit()
        finally:
            cancel_conn.close()

    canceller = threading.Thread(target=cancel_task, daemon=True)
    canceller.start()
    try:
        result = runner.run_executor(
            {
                "allowed_files": ["src/task_relay/**"],
                "auto_allowed_patterns": ["tests/**"],
                "instruction": "sleep until cancelled",
            }
        )
    finally:
        canceller.join(timeout=5)
        writer.close()

    row = conn.execute(
        """
        SELECT stage, success, exit_code, failure_code
        FROM tool_calls
        WHERE task_id = ?
        ORDER BY started_at DESC
        LIMIT 1
        """,
        ("task-cancel-mid-flight",),
    ).fetchone()
    task = queries.get_task(conn, "task-cancel-mid-flight")
    conn.close()

    assert result.ok is False
    assert result.failure_code == FailureCode.TOOL_INTERNAL_ERROR
    assert row is not None
    assert row["stage"] == Stage.EXECUTING.value
    assert row["success"] == 0
    assert row["exit_code"] != 0
    assert row["failure_code"] == FailureCode.TOOL_INTERNAL_ERROR.value
    assert task is not None
    assert task.state is TaskState.CANCELLED


def _insert_inbox_event(conn: sqlite3.Connection, event: CanonicalEvent) -> InboxEvent:
    inbox_event = InboxEvent(
        event_id=event.event_id,
        source=event.source,
        delivery_id=event.delivery_id,
        event_type=event.event_type,
        payload=event.payload,
        journal_offset=0,
        received_at=event.received_at,
    )
    queries.insert_event(conn, inbox_event)
    return inbox_event


def _sqlite_conn_factory(sqlite_path: Path) -> Callable[[], sqlite3.Connection]:
    def factory() -> sqlite3.Connection:
        conn = connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    return factory
