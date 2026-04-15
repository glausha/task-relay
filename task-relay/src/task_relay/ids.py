from __future__ import annotations

import secrets
import time
from hashlib import sha256
from uuid import UUID


def uuid7() -> UUID:
    timestamp_ms = time.time_ns() // 1_000_000
    random_bits = secrets.randbits(74)
    rand_a = random_bits >> 62
    rand_b = random_bits & ((1 << 62) - 1)
    value = (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return UUID(int=value)


def new_task_id() -> str:
    return str(uuid7())


def new_task_id_from_event(event_id: str) -> str:
    return str(UUID(bytes=sha256(event_id.encode("utf-8")).digest()[:16]))


def new_event_id() -> str:
    return str(uuid7())


def new_call_id() -> str:
    return str(uuid7())


def new_request_id() -> str:
    return str(uuid7())


# Why: outbox_id is delegated to SQLite AUTOINCREMENT.
