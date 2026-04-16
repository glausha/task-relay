from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


EXECUTOR_OUTPUT_CONTRACT = """Return a JSON object with this exact schema:
{
  "changed_files": ["<relative path>"],
  "summary": "<string>"
}
No prose, no markdown. Only JSON."""


def main() -> None:
    payload = json.loads(sys.stdin.read())
    protocol = payload.get("executor_child_protocol", "v1")
    del protocol

    if payload.get("_mock_response") is not None:
        result = payload["_mock_response"]
    else:
        from task_relay.runner.transports.claude_code_transport import ClaudeCodeTransport

        transport = ClaudeCodeTransport(timeout=payload.get("timeout", 600), role="executor")
        result = transport.request(
            request_id=payload.get("request_id"),
            payload=_executor_transport_payload(payload),
        )
    json.dump(result, sys.stdout)
    sys.stdout.flush()


def _executor_transport_payload(payload: dict[str, Any]) -> dict[str, Any]:
    worktree_path = Path(str(payload["worktree_path"]))
    plan_json = payload.get("plan_json", {})
    allowed_files = payload.get("allowed_files", [])
    auto_allowed_patterns = payload.get("auto_allowed_patterns", [])
    instruction_lines = [
        "You are the execution agent for task-relay.",
        "Modify files only within the allowed scope and return JSON only.",
        "",
        "Approved Plan JSON:",
        json.dumps(plan_json, ensure_ascii=False, indent=2, sort_keys=True),
        "",
        "Allowed Files:",
        json.dumps(allowed_files, ensure_ascii=False),
        "",
        "Auto Allowed Patterns:",
        json.dumps(auto_allowed_patterns, ensure_ascii=False),
    ]
    return {
        "instruction": "\n".join(instruction_lines),
        "cwd": worktree_path,
        "output_contract": EXECUTOR_OUTPUT_CONTRACT,
        "allowed_files": allowed_files,
        "auto_allowed_patterns": auto_allowed_patterns,
    }


if __name__ == "__main__":
    main()
