from __future__ import annotations

import hmac
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from task_relay.cli import cli
from task_relay.ingress.forgejo_webhook import canonicalize
from task_relay.journal.reader import JournalReader
from task_relay.journal.writer import JournalWriter
from task_relay.types import CanonicalEvent


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
