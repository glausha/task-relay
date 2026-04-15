from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from redis.exceptions import NoScriptError, ResponseError

from task_relay.clock import Clock, SystemClock
from task_relay.config import Settings

UTC = timezone.utc

LUA_ACQUIRE = """
-- KEYS[1] = lease:branch:<branch>
-- ARGV[1] = task_id, ARGV[2] = fencing_token, ARGV[3] = ttl_ms
if redis.call("EXISTS", KEYS[1]) == 1 then
    return 0
end
redis.call("SET", KEYS[1], cjson.encode({task_id = ARGV[1], fencing_token = tonumber(ARGV[2])}), "PX", tonumber(ARGV[3]))
return 1
"""

LUA_RENEW = """
-- KEYS[1] = lease key, ARGV[1] = task_id, ARGV[2] = fencing_token, ARGV[3] = ttl_ms
local v = redis.call("GET", KEYS[1])
if not v then return 0 end
local ok, decoded = pcall(cjson.decode, v)
if not ok then return 0 end
if decoded.task_id ~= ARGV[1] or tostring(decoded.fencing_token) ~= ARGV[2] then return 0 end
redis.call("PEXPIRE", KEYS[1], tonumber(ARGV[3]))
return 1
"""

LUA_RELEASE = """
-- KEYS[1], ARGV[1], ARGV[2]
local v = redis.call("GET", KEYS[1])
if not v then return 0 end
local ok, decoded = pcall(cjson.decode, v)
if not ok then return 0 end
if decoded.task_id ~= ARGV[1] or tostring(decoded.fencing_token) ~= ARGV[2] then return 0 end
redis.call("DEL", KEYS[1])
return 1
"""

LUA_ASSERT = """
local v = redis.call("GET", KEYS[1])
if not v then return 0 end
local ok, decoded = pcall(cjson.decode, v)
if not ok then return 0 end
if decoded.task_id ~= ARGV[1] or tostring(decoded.fencing_token) ~= ARGV[2] then return 0 end
if redis.call("PTTL", KEYS[1]) <= 0 then return 0 end
return 1
"""


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
        self._scripts: dict[str, str] = {}
        self._load_scripts()

    def acquire(
        self,
        *,
        branch: str,
        task_id: str,
        fencing_token: int,
        ttl_sec: int,
    ) -> LeaseHandle | None:
        key = self._key(branch)
        acquired = self._eval_script(
            "acquire",
            LUA_ACQUIRE,
            key,
            task_id,
            str(fencing_token),
            str(ttl_sec * 1000),
        )
        if acquired != 1:
            return None
        return self._build_handle(branch=branch, task_id=task_id, fencing_token=fencing_token)

    def renew(self, handle: LeaseHandle) -> LeaseHandle | None:
        renewed = self._eval_script(
            "renew",
            LUA_RENEW,
            self._key(handle.branch),
            handle.task_id,
            str(handle.fencing_token),
            str(self._settings.lease_ttl_seconds * 1000),
        )
        if renewed != 1:
            return None
        return self._build_handle(
            branch=handle.branch,
            task_id=handle.task_id,
            fencing_token=handle.fencing_token,
        )

    def release(self, handle: LeaseHandle) -> bool:
        released = self._eval_script(
            "release",
            LUA_RELEASE,
            self._key(handle.branch),
            handle.task_id,
            str(handle.fencing_token),
        )
        return released == 1

    def assert_readonly(self, branch: str, task_id: str, fencing_token: int) -> bool:
        result = self._eval_script(
            "assert",
            LUA_ASSERT,
            self._key(branch),
            task_id,
            str(fencing_token),
        )
        return result == 1

    def _key(self, branch: str) -> str:
        return f"lease:branch:{branch}"

    def _build_handle(self, *, branch: str, task_id: str, fencing_token: int) -> LeaseHandle:
        expires_at = self._expires_at_from_redis(self._key(branch))
        return LeaseHandle(
            branch=branch,
            task_id=task_id,
            fencing_token=fencing_token,
            expires_at=expires_at,
        )

    def _expires_at_from_redis(self, key: str) -> datetime:
        redis_time = self._client.time()
        pttl_ms = int(self._client.pttl(key))
        seconds = int(redis_time[0])
        microseconds = int(redis_time[1]) if len(redis_time) > 1 else 0
        base = datetime.fromtimestamp(seconds, tz=UTC) + timedelta(microseconds=microseconds)
        return base + timedelta(milliseconds=pttl_ms)

    def _load_scripts(self) -> None:
        # Keep SHA handles cached so each operation can use EVALSHA on the fast path.
        self._scripts = {
            "acquire": str(self._client.script_load(LUA_ACQUIRE)),
            "renew": str(self._client.script_load(LUA_RENEW)),
            "release": str(self._client.script_load(LUA_RELEASE)),
            "assert": str(self._client.script_load(LUA_ASSERT)),
        }

    def _eval_script(self, name: str, script: str, key: str, *args: str) -> int:
        try:
            result = self._client.evalsha(self._scripts[name], 1, key, *args)
        except NoScriptError:
            self._scripts[name] = str(self._client.script_load(script))
            result = self._client.evalsha(self._scripts[name], 1, key, *args)
        except ResponseError as exc:
            if "NOSCRIPT" not in str(exc):
                raise
            self._scripts[name] = str(self._client.script_load(script))
            result = self._client.evalsha(self._scripts[name], 1, key, *args)
        return int(result)
