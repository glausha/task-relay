from __future__ import annotations

import json
import sys
from typing import Any

from task_relay.runner.adapters.base import AdapterTransport


class StdinStdoutTransport(AdapterTransport):
    """Phase 3 swaps this stub for the real Anthropic transport."""

    def request(self, *, request_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        del request_id
        mock_response = payload.get("_mock_response")
        if isinstance(mock_response, dict):
            return mock_response
        return {"changed_files": [], "exit_code": 0}


def main() -> None:
    payload = json.loads(sys.stdin.read())
    transport = StdinStdoutTransport()
    result = transport.request(request_id=payload.get("request_id"), payload=payload)
    json.dump(result, sys.stdout)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
