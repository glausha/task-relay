from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from task_relay.db.queries import get_task
from task_relay.errors import LeaseError
from task_relay.runner.git_safety import (
    assert_lease_before_mutate,
    get_head_commit,
    push_feature_branch,
    update_head_commit,
)
from tests.unit._test_helpers import seed_task


class _StubRedisLease:
    def __init__(self, readonly: bool) -> None:
        self._readonly = readonly
        self.calls: list[tuple[str, str, int]] = []

    def assert_readonly(self, branch: str, task_id: str, fencing_token: int) -> bool:
        self.calls.append((branch, task_id, fencing_token))
        return self._readonly


def test_assert_lease_before_mutate_allows_current_holder() -> None:
    redis_lease = _StubRedisLease(readonly=True)

    assert_lease_before_mutate(redis_lease, branch="main", task_id="task-1", fencing_token=7)

    assert redis_lease.calls == [("main", "task-1", 7)]


def test_assert_lease_before_mutate_raises_when_lease_is_lost() -> None:
    redis_lease = _StubRedisLease(readonly=False)

    with pytest.raises(LeaseError, match="Lease lost for branch=main task=task-1 token=7"):
        assert_lease_before_mutate(redis_lease, branch="main", task_id="task-1", fencing_token=7)

    assert redis_lease.calls == [("main", "task-1", 7)]


def test_get_head_commit_returns_current_head(git_repo: Path) -> None:
    expected = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert get_head_commit(git_repo) == expected


def test_push_feature_branch_pushes_to_origin(git_repo: Path, tmp_path: Path) -> None:
    remote_repo = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote_repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(git_repo), "remote", "add", "origin", str(remote_repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(git_repo), "push", "-u", "origin", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-b", "task-relay/task-push"],
        check=True,
        capture_output=True,
    )

    push_feature_branch(git_repo, "task-relay/task-push")

    remote_ref = subprocess.run(
        ["git", "--git-dir", str(remote_repo), "show-ref", "--verify", "refs/heads/task-relay/task-push"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_ref.endswith("refs/heads/task-relay/task-push")


def test_update_head_commit_persists_last_known_head_commit(
    sqlite_conn: sqlite3.Connection,
    git_repo: Path,
) -> None:
    now = datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)
    seed_task(sqlite_conn, task_id="task-head", created_at=now)

    update_head_commit(sqlite_conn, "task-head", git_repo, now)
    sqlite_conn.commit()

    task = get_task(sqlite_conn, "task-head")
    assert task is not None
    assert task.last_known_head_commit == get_head_commit(git_repo)
