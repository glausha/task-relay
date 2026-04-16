from __future__ import annotations

import hmac
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from task_relay.cli import cli
from task_relay.db.connection import connect
from task_relay.db.queries import insert_outbox, upsert_task_on_create
from task_relay.ingress.forgejo_webhook import canonicalize
from task_relay.journal.reader import JournalReader
from task_relay.journal.writer import JournalWriter
from task_relay.types import CanonicalEvent, Stream, TaskState

from tests.unit._test_helpers import insert_plan_row, seed_task


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path | str]:
    sqlite_path = tmp_path / "var" / "state.sqlite"
    journal_dir = tmp_path / "var" / "journal"
    log_dir = tmp_path / "var" / "logs"
    monkeypatch.setenv("TASK_RELAY_SQLITE_PATH", str(sqlite_path))
    monkeypatch.setenv("TASK_RELAY_JOURNAL_DIR", str(journal_dir))
    monkeypatch.setenv("TASK_RELAY_LOG_DIR", str(log_dir))
    monkeypatch.setenv("TASK_RELAY_FORGEJO_WEBHOOK_SECRET", "top-secret")
    return {
        "sqlite_path": sqlite_path,
        "journal_dir": journal_dir,
        "log_dir": log_dir,
        "secret": "top-secret",
    }


def _read_journal_events(journal_dir: Path) -> list[CanonicalEvent]:
    reader = JournalReader(journal_dir)
    return [event for _, event in reader.iterate_from(None)]


def test_migrate_creates_db(cli_env: dict[str, Path | str]) -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["migrate"])

    assert result.exit_code == 0
    assert Path(cli_env["sqlite_path"]).exists()


def test_ingress_forgejo_single_ingest_appends_to_journal(cli_env: dict[str, Path | str], tmp_path: Path) -> None:
    runner = CliRunner()
    body_path = tmp_path / "issues-opened.json"
    body = {
        "action": "opened",
        "issue": {"id": 10, "number": 7, "title": "Example"},
        "repository": {"id": 20, "full_name": "org/repo"},
        "sender": {"id": 30},
    }
    body_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    body_path.write_bytes(body_bytes)
    signature = "sha256=" + hmac.new(str(cli_env["secret"]).encode("utf-8"), body_bytes, "sha256").hexdigest()

    result = runner.invoke(
        cli,
        [
            "ingress-forgejo",
            "--body",
            str(body_path),
            "--event",
            "issues",
            "--delivery-id",
            "delivery-1",
            "--signature",
            signature,
        ],
    )

    events = _read_journal_events(Path(cli_env["journal_dir"]))

    assert result.exit_code == 0
    assert list(Path(cli_env["journal_dir"]).glob("*.ndjson.zst"))
    assert len(events) == 1


def test_approve_appends_cli_event_to_journal(cli_env: dict[str, Path | str]) -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["approve", "--task", "task-X", "--actor", "1"])

    events = _read_journal_events(Path(cli_env["journal_dir"]))

    assert result.exit_code == 0
    assert "Accepted: request_id=" in result.output
    assert len(events) == 1
    assert events[0].event_type == "/approve"


def test_migrate_ingester_router_status_flow_shows_planning(cli_env: dict[str, Path | str]) -> None:
    runner = CliRunner()

    migrate_result = runner.invoke(cli, ["migrate"])
    assert migrate_result.exit_code == 0

    body = {
        "action": "opened",
        "issue": {"id": 101, "number": 9, "title": "Flow"},
        "repository": {"id": 201, "full_name": "org/repo"},
        "sender": {"id": 301},
    }
    event = canonicalize("issues", "delivery-flow", body)
    assert event is not None
    writer = JournalWriter(Path(cli_env["journal_dir"]))
    try:
        writer.append(event)
    finally:
        writer.close()

    ingester_result = runner.invoke(cli, ["ingester", "--once"])
    router_result = runner.invoke(cli, ["router", "--once"])
    status_result = runner.invoke(cli, ["status"])

    assert ingester_result.exit_code == 0
    assert ingester_result.output.strip() == "1"
    assert router_result.exit_code == 0
    assert router_result.output.strip() == "1"
    assert status_result.exit_code == 0
    assert "planning=1" in status_result.output


def test_projection_rebuild_cli_echoes_rebuilt_row_count(cli_env: dict[str, Path | str]) -> None:
    runner = CliRunner()
    migrate_result = runner.invoke(cli, ["migrate"])
    assert migrate_result.exit_code == 0

    conn = connect(Path(cli_env["sqlite_path"]))
    try:
        now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
        task = seed_task(
            conn,
            task_id="task-cli-rebuild",
            source_issue_id="42",
            created_at=now,
            state=TaskState.DONE,
            state_rev=2,
        )
        insert_plan_row(
            conn,
            task_id=task.task_id,
            plan_rev=1,
            plan_json={"acceptance_criteria": ["works"]},
            created_at=now,
        )
    finally:
        conn.close()

    result = runner.invoke(cli, ["projection-rebuild", "--task", "task-cli-rebuild"])

    assert result.exit_code == 0
    assert "Rebuilt " in result.output
    assert "outbox rows for task-cli-rebuild" in result.output


def test_projection_cli_dry_run_once_processes_outbox(cli_env: dict[str, Path | str]) -> None:
    runner = CliRunner()
    migrate_result = runner.invoke(cli, ["migrate"])
    assert migrate_result.exit_code == 0

    conn = connect(Path(cli_env["sqlite_path"]))
    try:
        now = datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)
        upsert_task_on_create(
            conn,
            task_id="task-cli-projection",
            source_issue_id="42",
            requested_by="discord:42",
            notification_target="42",
            created_at=now,
            updated_at=now,
        )
        outbox_id = insert_outbox(
            conn,
            task_id="task-cli-projection",
            stream=Stream.TASK_COMMENT,
            target="42",
            origin_event_id="event-1",
            payload_json=json.dumps({"body": "hello", "issue_number": 42}),
            state_rev=1,
            idempotency_key="cli-projection-idem",
            next_attempt_at=now.isoformat(),
        )
    finally:
        conn.close()

    result = runner.invoke(cli, ["projection", "--dry-run", "--once"])

    conn = connect(Path(cli_env["sqlite_path"]))
    try:
        row = conn.execute(
            "SELECT sent_at FROM projection_outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
    finally:
        conn.close()

    assert result.exit_code == 0
    assert result.output.strip() == "1"
    assert row is not None
    assert row["sent_at"] is not None


def test_retention_cli_json_outputs_dict(cli_env: dict[str, Path | str]) -> None:
    runner = CliRunner()
    migrate_result = runner.invoke(cli, ["migrate"])
    assert migrate_result.exit_code == 0

    result = runner.invoke(cli, ["retention", "--scope", "all", "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert isinstance(payload, dict)
    assert "journal_deleted" in payload
    assert "log" in payload
