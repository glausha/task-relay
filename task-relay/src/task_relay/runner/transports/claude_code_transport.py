from __future__ import annotations

import json
import subprocess
from typing import Any

from task_relay.errors import FailureCode, TimeoutTransportError, UnknownTransportError


class ClaudeCodeTransport:
    """Claude Code CLI transport for Executor: basic-design §0 エージェント構成。"""

    def __init__(self, *, timeout: int = 600) -> None:
        self._timeout = timeout

    def request(self, *, request_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        del request_id
        instruction = payload.get("instruction", json.dumps(payload))
        try:
            result = subprocess.run(
                ["claude", "--print", "--output-format", "json", "-p", instruction],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=payload.get("worktree_path"),
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
        except json.JSONDecodeError:
            output = {"raw_output": result.stdout[:2000]}
        if not isinstance(output, dict):
            output = {"raw_output": result.stdout[:2000]}
        return {
            "changed_files": output.get("changed_files", []) if isinstance(output.get("changed_files"), list) else [],
            "exit_code": result.returncode,
            **output,
        }
