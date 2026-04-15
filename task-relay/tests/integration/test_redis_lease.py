from __future__ import annotations

from dataclasses import replace
import time
from datetime import timezone

import fakeredis

from task_relay.branch_lease.redis_lease import RedisLease
from task_relay.config import Settings

UTC = timezone.utc


def test_redis_lease_round_trip() -> None:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    lease = RedisLease(client, Settings(lease_ttl_seconds=2))

    handle = lease.acquire(branch="main", task_id="task-1", fencing_token=7, ttl_sec=2)

    assert handle is not None
    assert handle.expires_at.tzinfo == UTC
    assert lease.assert_readonly("main", "task-1", 7) is True

    renewed = lease.renew(handle)

    assert renewed is not None
    assert renewed.branch == "main"
    assert renewed.task_id == "task-1"
    assert renewed.fencing_token == 7
    assert renewed.expires_at >= handle.expires_at
    assert lease.assert_readonly("main", "task-1", 7) is True
    assert lease.release(renewed) is True
    assert client.get("lease:branch:main") is None


def test_acquire_fails_for_different_task_when_branch_is_leased() -> None:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    lease = RedisLease(client, Settings(lease_ttl_seconds=2))

    first = lease.acquire(branch="main", task_id="task-1", fencing_token=7, ttl_sec=2)
    second = lease.acquire(branch="main", task_id="task-2", fencing_token=8, ttl_sec=2)

    assert first is not None
    assert second is None


def test_renew_fails_for_different_fencing_token() -> None:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    lease = RedisLease(client, Settings(lease_ttl_seconds=2))

    handle = lease.acquire(branch="main", task_id="task-1", fencing_token=7, ttl_sec=2)

    assert handle is not None
    assert lease.renew(replace(handle, fencing_token=8)) is None


def test_assert_readonly_turns_false_after_ttl_expires() -> None:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    lease = RedisLease(client, Settings(lease_ttl_seconds=1))

    handle = lease.acquire(branch="main", task_id="task-1", fencing_token=7, ttl_sec=1)

    assert handle is not None
    assert lease.assert_readonly("main", "task-1", 7) is True

    client.pexpire("lease:branch:main", 0)
    time.sleep(0.01)

    assert lease.assert_readonly("main", "task-1", 7) is False
