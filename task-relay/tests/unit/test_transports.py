from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from task_relay.errors import FailureCode, TimeoutTransportError, UnknownTransportError
from task_relay.runner.transports.claude_code_transport import ClaudeCodeTransport
from task_relay.runner.transports.codex_transport import CodexTransport


def test_codex_transport_returns_decoded_json(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(
        args=["codex"],
        returncode=0,
        stdout='{"decision":"pass","criteria":[]}',
        stderr="",
    )
    run_mock = Mock(return_value=completed)
    monkeypatch.setattr("task_relay.runner.transports.codex_transport.subprocess.run", run_mock)
    transport = CodexTransport(model="gpt-5.4")

    result = transport.request(request_id="req-1", payload={"task_id": "task-1"})

    assert result == {"decision": "pass", "criteria": []}
    run_mock.assert_called_once()


def test_codex_transport_maps_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(args=["codex"], returncode=0, stdout="oops", stderr="")
    monkeypatch.setattr("task_relay.runner.transports.codex_transport.subprocess.run", Mock(return_value=completed))
    transport = CodexTransport()

    with pytest.raises(UnknownTransportError) as exc_info:
        transport.request(request_id=None, payload={"task_id": "task-1"})

    assert exc_info.value.failure_code == FailureCode.INVALID_REVIEW_OUTPUT


def test_codex_transport_maps_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    run_mock = Mock(side_effect=subprocess.TimeoutExpired(cmd=["codex"], timeout=120))
    monkeypatch.setattr("task_relay.runner.transports.codex_transport.subprocess.run", run_mock)
    transport = CodexTransport()

    with pytest.raises(TimeoutTransportError):
        transport.request(request_id=None, payload={"task_id": "task-1"})


def test_claude_code_transport_uses_planner_role_and_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess(
        args=["claude"],
        returncode=0,
        stdout='{"goal":"ok","sub_tasks":[],"allowed_files":[],"auto_allowed_patterns":[],"acceptance_criteria":[],"forbidden_changes":[],"risk_notes":[]}',
        stderr="",
    )
    run_mock = Mock(return_value=completed)
    monkeypatch.setattr("task_relay.runner.transports.claude_code_transport.subprocess.run", run_mock)
    transport = ClaudeCodeTransport(timeout=42, role="planner")

    result = transport.request(
        request_id="req-1",
        payload={
            "instruction": "plan this task",
            "cwd": tmp_path,
            "output_contract": "Return JSON.",
        },
    )

    assert result["goal"] == "ok"
    run_mock.assert_called_once()
    assert run_mock.call_args.args[0] == ["claude", "--print", "--output-format", "json", "-p", "plan this task\n\nReturn JSON."]
    assert run_mock.call_args.kwargs["cwd"] == str(tmp_path)
    assert run_mock.call_args.kwargs["timeout"] == 42


def test_claude_code_transport_uses_executor_role_and_changed_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess(
        args=["claude"],
        returncode=0,
        stdout='{"changed_files":["src/task_relay/x.py"],"summary":"done"}',
        stderr="",
    )
    run_mock = Mock(return_value=completed)
    monkeypatch.setattr("task_relay.runner.transports.claude_code_transport.subprocess.run", run_mock)
    transport = ClaudeCodeTransport(role="executor")

    result = transport.request(
        request_id=None,
        payload={"instruction": "apply fix", "cwd": tmp_path},
    )

    assert result == {
        "changed_files": ["src/task_relay/x.py"],
        "exit_code": 0,
        "summary": "done",
    }
    assert run_mock.call_args.kwargs["cwd"] == str(tmp_path)


def test_claude_code_transport_maps_invalid_json_for_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout="plain text", stderr="")
    monkeypatch.setattr(
        "task_relay.runner.transports.claude_code_transport.subprocess.run",
        Mock(return_value=completed),
    )
    transport = ClaudeCodeTransport(role="planner")

    with pytest.raises(UnknownTransportError) as exc_info:
        transport.request(request_id=None, payload={"instruction": "plan"})

    assert exc_info.value.failure_code == FailureCode.INVALID_PLAN_OUTPUT
    assert exc_info.value.raw_text == "plain text"


def test_claude_code_transport_maps_invalid_json_for_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout="plain text", stderr="")
    monkeypatch.setattr(
        "task_relay.runner.transports.claude_code_transport.subprocess.run",
        Mock(return_value=completed),
    )
    transport = ClaudeCodeTransport(role="executor")

    with pytest.raises(UnknownTransportError) as exc_info:
        transport.request(request_id=None, payload={"instruction": "execute"})

    assert exc_info.value.failure_code == FailureCode.TOOL_INTERNAL_ERROR


def test_claude_code_transport_maps_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(args=["claude"], returncode=1, stdout="", stderr="failed")
    monkeypatch.setattr(
        "task_relay.runner.transports.claude_code_transport.subprocess.run",
        Mock(return_value=completed),
    )
    transport = ClaudeCodeTransport()

    with pytest.raises(UnknownTransportError) as exc_info:
        transport.request(request_id=None, payload={"instruction": "apply fix"})

    assert exc_info.value.failure_code == FailureCode.TOOL_INTERNAL_ERROR
    assert exc_info.value.raw_text == "failed"
