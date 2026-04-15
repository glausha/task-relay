from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

import structlog


REDACT_KEYS = {
    "authorization",
    "cookie",
    "token",
    "secret",
    "private_key",
    "content",
}
_TASK_ID: ContextVar[str | None] = ContextVar("task_id", default=None)


def bind_task_id(task_id: str | None) -> None:
    _TASK_ID.set(task_id)


def _with_task_id(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    current = _TASK_ID.get()
    if current is not None:
        event_dict.setdefault("task_id", current)
    return event_dict


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if key.lower() in REDACT_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def redact_processor(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    return _redact(event_dict)


def setup_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _with_task_id,
            redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
