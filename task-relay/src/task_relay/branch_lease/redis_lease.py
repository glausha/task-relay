from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from redis.exceptions import WatchError

from task_relay.clock import Clock, SystemClock
from task_relay.config import Settings


@dataclass(frozen=True)
class LeaseHandle:
    branch: str
    task_id: str
    fencing_token: int
    expires_at: datetime


class RedisLease:
    def __init__(self, client: Any, settings: Settings, clock: Clock = SystemClock()) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock

    def acquire(
        self,
        *,
        branch: str,
        task_id: str,
        fencing_token: int,
        ttl_sec: int,
    ) -> LeaseHandle | None:
        expires_at = self._clock.now() + timedelta(seconds=ttl_sec)
        acquired = self._client.set(
            self._key(branch),
            self._encode_value(task_id=task_id, fencing_token=fencing_token, expires_at=expires_at),
            nx=True,
            px=ttl_sec * 1000,
        )
        if not acquired:
            return None
        return LeaseHandle(
            branch=branch,
            task_id=task_id,
            fencing_token=fencing_token,
            expires_at=expires_at,
        )

    def renew(self, handle: LeaseHandle) -> LeaseHandle | None:
        ttl_sec = self._settings.lease_ttl_seconds
        key = self._key(handle.branch)
        while True:
            with self._client.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    current = self._decode_value(pipe.get(key))
                    if current is None or not self._same_owner(current, handle.task_id, handle.fencing_token):
                        pipe.unwatch()
                        return None
                    expires_at = self._clock.now() + timedelta(seconds=ttl_sec)
                    pipe.multi()
                    pipe.set(
                        key,
                        self._encode_value(
                            task_id=handle.task_id,
                            fencing_token=handle.fencing_token,
                            expires_at=expires_at,
                        ),
                        xx=True,
                        px=ttl_sec * 1000,
                    )
                    result = pipe.execute()
                    if result and result[0]:
                        return LeaseHandle(
                            branch=handle.branch,
                            task_id=handle.task_id,
                            fencing_token=handle.fencing_token,
                            expires_at=expires_at,
                        )
                    return None
                except WatchError:
                    continue

    def release(self, handle: LeaseHandle) -> bool:
        key = self._key(handle.branch)
        while True:
            with self._client.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    current = self._decode_value(pipe.get(key))
                    if current is None or not self._same_owner(current, handle.task_id, handle.fencing_token):
                        pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.delete(key)
                    result = pipe.execute()
                    return bool(result and result[0] == 1)
                except WatchError:
                    continue

    def assert_readonly(self, branch: str, task_id: str, fencing_token: int) -> bool:
        current = self._decode_value(self._client.get(self._key(branch)))
        if current is None or not self._same_owner(current, task_id, fencing_token):
            return False
        expires_at_epoch = current.get("expires_at_epoch")
        if not isinstance(expires_at_epoch, (int, float)):
            return False
        expires_at = datetime.fromtimestamp(expires_at_epoch, tz=timezone.utc)
        return expires_at > self._clock.now()

    def _key(self, branch: str) -> str:
        return f"lease:branch:{branch}"

    def _encode_value(self, *, task_id: str, fencing_token: int, expires_at: datetime) -> str:
        return json.dumps(
            {
                "task_id": task_id,
                "fencing_token": fencing_token,
                "expires_at_epoch": expires_at.astimezone(timezone.utc).timestamp(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def _decode_value(self, raw: Any) -> dict[str, Any] | None:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None

    def _same_owner(self, value: dict[str, Any], task_id: str, fencing_token: int) -> bool:
        return value.get("task_id") == task_id and value.get("fencing_token") == fencing_token
