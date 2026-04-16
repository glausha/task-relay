"""Git mutate safety: detailed-design §5.3."""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from task_relay.branch_lease.redis_lease import RedisLease
from task_relay.errors import LeaseError


def assert_lease_before_mutate(
    redis_lease: RedisLease,
    *,
    branch: str,
    task_id: str,
    fencing_token: int,
) -> None:
    """Git mutate 前に lease を検査。失敗で LeaseError raise。"""
    if not redis_lease.assert_readonly(branch, task_id, fencing_token):
        raise LeaseError(f"Lease lost for branch={branch} task={task_id} token={fencing_token}")


def get_head_commit(worktree_path: Path) -> str:
    """worktree の HEAD commit sha を返す。"""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def push_feature_branch(worktree_path: Path, feature_branch: str) -> None:
    """feature branch を remote に push する。"""
    subprocess.run(
        ["git", "push", "origin", feature_branch],
        cwd=worktree_path,
        capture_output=True,
        check=True,
    )


def update_head_commit(
    conn: sqlite3.Connection,
    task_id: str,
    worktree_path: Path,
    updated_at: datetime,
) -> None:
    """Git mutate 成功直後に last_known_head_commit を更新する。"""
    from task_relay.db.queries import update_last_known_head_commit

    head = get_head_commit(worktree_path)
    update_last_known_head_commit(conn, task_id, head, updated_at)
