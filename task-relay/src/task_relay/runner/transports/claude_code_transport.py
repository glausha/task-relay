from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from task_relay.errors import FailureCode, TimeoutTransportError, UnknownTransportError


class ClaudeCodeTransport:
    """Claude Code CLI transport for Planner / Executor: basic-design §0 エージェント構成。"""

    def __init__(self, *, timeout: int = 600, role: str = "executor") -> None:
        self._timeout = timeout
        self._role = role

    def request(self, *, request_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        del request_id
        instruction = self._instruction_from_payload(payload)
        cwd = self._cwd_from_payload(payload)
        try:
            result = subprocess.run(
                ["claude", "--print", "--output-format", "json", "-p", instruction],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=None if cwd is None else str(cwd),
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutTransportError(raw_text=instruction) from exc
        except OSError as exc:
            raise UnknownTransportError(
                FailureCode.TOOL_INTERNAL_ERROR,
                str(exc),
                raw_text=str(exc),
            ) from exc
        if result.returncode != 0:
            raise UnknownTransportError(
                FailureCode.TOOL_INTERNAL_ERROR,
                raw_text=result.stderr[:500] or result.stdout[:500],
            )
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise UnknownTransportError(
                self._invalid_output_failure_code(),
                raw_text=result.stdout[:2000],
            ) from exc
        if not isinstance(output, dict):
            raise UnknownTransportError(
                self._invalid_output_failure_code(),
                raw_text=result.stdout[:2000],
            )
        return {
            "changed_files": output.get("changed_files", []) if isinstance(output.get("changed_files"), list) else [],
            "exit_code": result.returncode,
            **output,
        }

    def _instruction_from_payload(self, payload: dict[str, Any]) -> str:
        instruction = payload.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            instruction = json.dumps(payload, ensure_ascii=False)
        output_contract = payload.get("output_contract")
        if not isinstance(output_contract, str) or not output_contract.strip():
            return instruction
        # WHY: Claude Code CLI has a single prompt channel, so the schema contract must be embedded in-band.
        return f"{instruction.rstrip()}\n\n{output_contract.strip()}"

    def _cwd_from_payload(self, payload: dict[str, Any]) -> Path | None:
        cwd = payload.get("cwd")
        if cwd is None:
            cwd = payload.get("worktree_path")
        if cwd is None:
            return None
        return Path(cwd)

    def _invalid_output_failure_code(self) -> FailureCode:
        if self._role == "planner":
            return FailureCode.INVALID_PLAN_OUTPUT
        return FailureCode.TOOL_INTERNAL_ERROR
