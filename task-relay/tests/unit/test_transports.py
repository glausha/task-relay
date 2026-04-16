from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from task_relay.errors import (
    FailureCode,
    FatalTransportError,
    TimeoutTransportError,
    TransientTransportError,
    UnknownTransportError,
)
from task_relay.runner.transports import anthropic_transport
from task_relay.runner.transports.anthropic_transport import AnthropicTransport
from task_relay.runner.transports.claude_code_transport import ClaudeCodeTransport
from task_relay.runner.transports.codex_transport import CodexTransport


def test_anthropic_sdk_importable() -> None:
    pytest.importorskip("anthropic")


def test_anthropic_transport_returns_decoded_json(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, client = _build_anthropic_transport(monkeypatch)
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(text='{"goal":"ok"}')])

    result = transport.request(request_id="req-1", payload={"task_id": "task-1"})

    assert result == {"goal": "ok"}
    client.messages.create.assert_called_once()


def test_anthropic_transport_maps_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, client = _build_anthropic_transport(monkeypatch)
    error = _FakeRateLimitError("busy")
    client.messages.create.side_effect = error

    with pytest.raises(TransientTransportError) as exc_info:
        transport.request(request_id="req-1", payload={"task_id": "task-1"})

    assert exc_info.value.failure_code == FailureCode.RATE_LIMITED


def test_anthropic_transport_maps_authentication_error(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, client = _build_anthropic_transport(monkeypatch)
    client.messages.create.side_effect = _FakeAuthenticationError("bad key")

    with pytest.raises(FatalTransportError) as exc_info:
        transport.request(request_id="req-1", payload={"task_id": "task-1"})

    assert exc_info.value.failure_code == FailureCode.AUTH_ERROR


def test_anthropic_transport_maps_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, client = _build_anthropic_transport(monkeypatch)
    client.messages.create.side_effect = _FakeAPITimeoutError("timed out")

    with pytest.raises(TimeoutTransportError):
        transport.request(request_id="req-1", payload={"task_id": "task-1"})


def test_anthropic_transport_maps_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, client = _build_anthropic_transport(monkeypatch)
    client.messages.create.side_effect = _FakeAPIError("api failure")

    with pytest.raises(UnknownTransportError) as exc_info:
        transport.request(request_id="req-1", payload={"task_id": "task-1"})

    assert exc_info.value.failure_code == FailureCode.TOOL_INTERNAL_ERROR


def test_anthropic_transport_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, client = _build_anthropic_transport(monkeypatch)
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(text="not json")])

    with pytest.raises(UnknownTransportError) as exc_info:
        transport.request(request_id="req-1", payload={"task_id": "task-1"})

    assert exc_info.value.failure_code == FailureCode.INVALID_PLAN_OUTPUT
    assert exc_info.value.raw_text == "not json"


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


def test_claude_code_transport_returns_decoded_json(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    completed = subprocess.CompletedProcess(
        args=["claude"],
        returncode=0,
        stdout='{"changed_files":["src/task_relay/x.py"],"summary":"done"}',
        stderr="",
    )
    run_mock = Mock(return_value=completed)
    monkeypatch.setattr("task_relay.runner.transports.claude_code_transport.subprocess.run", run_mock)
    transport = ClaudeCodeTransport(timeout=42)

    result = transport.request(
        request_id="req-1",
        payload={"instruction": "apply fix", "worktree_path": str(tmp_path)},
    )

    assert result == {
        "changed_files": ["src/task_relay/x.py"],
        "exit_code": 0,
        "summary": "done",
    }
    run_mock.assert_called_once()
    assert run_mock.call_args.kwargs["cwd"] == str(tmp_path)
    assert run_mock.call_args.kwargs["timeout"] == 42


def test_claude_code_transport_falls_back_to_raw_output(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout="plain text", stderr="")
    monkeypatch.setattr(
        "task_relay.runner.transports.claude_code_transport.subprocess.run",
        Mock(return_value=completed),
    )
    transport = ClaudeCodeTransport()

    result = transport.request(request_id=None, payload={"instruction": "apply fix"})

    assert result["changed_files"] == []
    assert result["exit_code"] == 0
    assert result["raw_output"] == "plain text"


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


def _build_anthropic_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AnthropicTransport, SimpleNamespace]:
    client = SimpleNamespace(messages=SimpleNamespace(create=Mock()))
    fake_anthropic = SimpleNamespace(
        Anthropic=lambda: client,
        RateLimitError=_FakeRateLimitError,
        AuthenticationError=_FakeAuthenticationError,
        APITimeoutError=_FakeAPITimeoutError,
        APIError=_FakeAPIError,
    )
    monkeypatch.setattr(anthropic_transport, "anthropic", fake_anthropic)
    monkeypatch.setattr(anthropic_transport, "_ANTHROPIC_IMPORT_ERROR", None)
    return AnthropicTransport(), client


class _FakeRateLimitError(Exception):
    pass


class _FakeAuthenticationError(Exception):
    pass


class _FakeAPITimeoutError(Exception):
    pass


class _FakeAPIError(Exception):
    pass
