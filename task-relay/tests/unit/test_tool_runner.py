from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from task_relay.clock import FrozenClock
from task_relay.config import Settings
from task_relay.db.connection import connect
from task_relay.db.queries import get_task
from task_relay.journal.writer import JournalWriter
from task_relay.runner.adapters.base import TimeoutDecision
from task_relay.runner.tool_runner import ToolRunner, decide_timeout_retry
from task_relay.types import AdapterContract, Stage, TaskState

from tests.unit._test_helpers import seed_task


def test_decide_timeout_retry_retries_planning_once_with_request_id_support() -> None:
    contract = AdapterContract("planner", "v1", True)

    assert decide_timeout_retry(stage=Stage.PLANNING, contract=contract, attempt_count=0) == TimeoutDecision.RETRY


def test_decide_timeout_retry_requires_human_review_without_request_id_support() -> None:
    contract = AdapterContract("planner", "v1", False)

    assert (
        decide_timeout_retry(stage=Stage.PLANNING, contract=contract, attempt_count=0)
        == TimeoutDecision.GIVE_UP_HR
    )


def test_decide_timeout_retry_requires_human_review_after_retry_budget_is_spent() -> None:
    contract = AdapterContract("planner", "v1", True)

    assert decide_timeout_retry(stage=Stage.PLANNING, contract=contract, attempt_count=1) == TimeoutDecision.GIVE_UP_HR


def test_decide_timeout_retry_requires_human_review_during_execution() -> None:
    contract = AdapterContract("executor", "v1", True)

    assert decide_timeout_retry(stage=Stage.EXECUTING, contract=contract, attempt_count=0) == TimeoutDecision.GIVE_UP_HR


def test_decide_timeout_retry_retries_reviewing_once_with_request_id_support() -> None:
    contract = AdapterContract("reviewer", "v1", True)

    assert decide_timeout_retry(stage=Stage.REVIEWING, contract=contract, attempt_count=0) == TimeoutDecision.RETRY


def test_observe_state_change_implementing_creates_worktree_and_updates_task(
    sqlite_conn: sqlite3.Connection,
    git_repo: Path,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    seed_task(
        sqlite_conn,
        task_id="task-impl",
        created_at=now,
        state=TaskState.PLAN_APPROVED,
        lease_branch="main",
    )
    runner = ToolRunner(
        "task-impl",
        _conn_factory(sqlite_conn),
        JournalWriter(tmp_path / "journal", FrozenClock(now)),
        redis_client=object(),
        settings=Settings(executor_workspace_root=tmp_path / "workspaces"),
        repo_root=git_repo,
        clock=FrozenClock(now),
    )

    runner.observe_state_change(TaskState.IMPLEMENTING)

    task = get_task(sqlite_conn, "task-impl")
    assert task is not None
    assert task.lease_branch == "main"
    assert task.feature_branch == "task-relay/task-impl"
    assert task.worktree_path == str(tmp_path / "workspaces" / "task-impl")
    assert Path(task.worktree_path).is_dir()


def test_observe_state_change_done_cleans_up_worktree(
    sqlite_conn: sqlite3.Connection,
    git_repo: Path,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    seed_task(
        sqlite_conn,
        task_id="task-done",
        created_at=now,
        state=TaskState.IMPLEMENTING,
        lease_branch="main",
    )
    runner = ToolRunner(
        "task-done",
        _conn_factory(sqlite_conn),
        JournalWriter(tmp_path / "journal", FrozenClock(now)),
        redis_client=object(),
        settings=Settings(executor_workspace_root=tmp_path / "workspaces"),
        repo_root=git_repo,
        clock=FrozenClock(now),
    )
    runner.observe_state_change(TaskState.IMPLEMENTING)
    task = get_task(sqlite_conn, "task-done")
    assert task is not None
    assert task.worktree_path is not None

    runner.observe_state_change(TaskState.DONE)

    branch_result = subprocess.run(
        ["git", "-C", str(git_repo), "branch", "--list", "task-relay/task-done"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert not Path(task.worktree_path).exists()
    assert branch_result.stdout.strip() == ""


def _conn_factory(sqlite_conn: sqlite3.Connection) -> Callable[[], sqlite3.Connection]:
    row = sqlite_conn.execute("PRAGMA database_list").fetchone()
    assert row is not None
    db_path = Path(row["file"])

    def factory() -> sqlite3.Connection:
        conn = connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return factory
