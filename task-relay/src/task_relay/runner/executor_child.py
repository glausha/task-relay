from __future__ import annotations

import json
import sys


def main() -> None:
    payload = json.loads(sys.stdin.read())
    protocol = payload.get("executor_child_protocol", "v1")
    del protocol

    if payload.get("_mock_response") is not None:
        result = payload["_mock_response"]
    else:
        from task_relay.runner.transports.claude_code_transport import ClaudeCodeTransport

        transport = ClaudeCodeTransport(timeout=payload.get("timeout", 600))
        result = transport.request(request_id=payload.get("request_id"), payload=payload)
    json.dump(result, sys.stdout)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
