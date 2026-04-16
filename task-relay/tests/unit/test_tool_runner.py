from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import fakeredis
import pytest

from task_relay.branch_lease.redis_lease import RedisLease
from task_relay.clock import FrozenClock
from task_relay.config import Settings
from task_relay.db.connection import connect
from task_relay.db.queries import get_task
from task_relay.errors import LeaseError, TimeoutTransportError
from task_relay.journal.writer import JournalWriter
from task_relay.runner.adapters.planner import PlannerAdapter
from task_relay.runner.adapters.reviewer import ReviewerAdapter
from task_relay.runner.adapters.base import TimeoutDecision
from task_relay.runner.tool_runner import ToolRunner, decide_timeout_retry
from task_relay.types import AdapterContract, JournalPosition, Plan, Stage, TaskState

from tests.unit._fake_transports import FakeTransport
from tests.unit._test_helpers import seed_task


def test_decide_timeout_retry_retries_planning_once_with_request_id_support() -> None:
    contract = AdapterContract("planner", "v1", True)

    assert decide_timeout_retry(stage=Stage.PLANNING, contract=contract, attempt_count=0) == TimeoutDecision.RETRY


def test_decide_timeout_retry_requires_human_review_without_request_id_support() -> None:
    contract = AdapterContract("planner", "v2", False)

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


def test_run_planning_records_tool_call(
    sqlite_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    seed_task(sqlite_conn, task_id="task-plan", created_at=now, state=TaskState.PLANNING)
    planner = PlannerAdapter(FakeTransport([{"payload": _valid_plan_json(), "tokens_in": 11, "tokens_out": 22}]))
    runner = ToolRunner(
        "task-plan",
        _conn_factory(sqlite_conn),
        JournalWriter(tmp_path / "journal", FrozenClock(now)),
        redis_client=object(),
        settings=Settings(log_dir=tmp_path / "logs", executor_workspace_root=tmp_path / "workspaces"),
        repo_root=tmp_path,
        planner=planner,
        clock=FrozenClock(now),
    )

    result = runner.run_planning({"goal": "make a plan", "repo_context": "task_id=task-plan"})

    row = sqlite_conn.execute(
        "SELECT stage, tool_name, success, failure_code, tokens_in, tokens_out FROM tool_calls"
    ).fetchone()
    assert row is not None
    assert result.ok is True
    assert row["stage"] == Stage.PLANNING.value
    assert row["tool_name"] == "planner"
    assert row["success"] == 1
    assert row["failure_code"] is None
    assert row["tokens_in"] == 11
    assert row["tokens_out"] == 22


def test_run_planning_timeout_appends_internal_event(
    sqlite_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    seed_task(sqlite_conn, task_id="task-plan-timeout", created_at=now, state=TaskState.PLANNING)
    transport = FakeTransport(
        [{}, {}],
        errors=[TimeoutTransportError("timed out"), TimeoutTransportError("timed out")],
    )
    planner = PlannerAdapter(transport)
    journal_writer = Mock()
    journal_writer.append.return_value = JournalPosition(file="20260415.zst", offset=0)
    runner = ToolRunner(
        "task-plan-timeout",
        _conn_factory(sqlite_conn),
        journal_writer,
        redis_client=object(),
        settings=Settings(log_dir=tmp_path / "logs", executor_workspace_root=tmp_path / "workspaces"),
        repo_root=tmp_path,
        planner=planner,
        clock=FrozenClock(now),
    )

    try:
        runner.run_planning({"goal": "make a plan", "repo_context": "task_id=task-plan-timeout"})
    except TimeoutTransportError:
        pass
    else:
        raise AssertionError("expected planner timeout to be raised")

    assert transport.call_count == 1
    journal_writer.append.assert_called_once()
    event = journal_writer.append.call_args.args[0]
    assert event.event_type == "internal.planner_timeout"
    assert event.payload["task_id"] == "task-plan-timeout"
    assert event.payload["failure_code"] == "timeout"


def test_run_executor_records_tool_call(
    sqlite_conn: sqlite3.Connection,
    git_repo: Path,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    seed_task(
        sqlite_conn,
        task_id="task-exec",
        created_at=now,
        state=TaskState.IMPLEMENTING,
        lease_branch="main",
    )
    settings = Settings(
        log_dir=tmp_path / "logs",
        executor_workspace_root=tmp_path / "workspaces",
        lease_ttl_seconds=30,
        lease_renew_interval_seconds=1,
    )
    redis_client = fakeredis.FakeStrictRedis(decode_responses=True)
    lease = RedisLease(redis_client, settings, FrozenClock(now))
    lease_handle = lease.acquire(branch="main", task_id="task-exec", fencing_token=7, ttl_sec=30)
    assert lease_handle is not None
    runner = ToolRunner(
        "task-exec",
        _conn_factory(sqlite_conn),
        JournalWriter(tmp_path / "journal", FrozenClock(now)),
        redis_client=redis_client,
        settings=settings,
        repo_root=git_repo,
        lease_handle=lease_handle,
        fencing_token=7,
        clock=FrozenClock(now),
    )
    runner.setup_worktree(lease_branch="main")

    result = runner.run_executor(
        {
            "allowed_files": ["src/task_relay/**"],
            "auto_allowed_patterns": ["tests/**"],
            "_mock_response": {"changed_files": ["src/task_relay/runner/tool_runner.py"], "exit_code": 0},
        }
    )

    row = sqlite_conn.execute(
        "SELECT stage, tool_name, success, exit_code, failure_code FROM tool_calls"
    ).fetchone()
    assert row is not None
    assert result.ok is True
    assert result.payload["in_scope_files"] == ["src/task_relay/runner/tool_runner.py"]
    assert result.payload["out_of_scope_files"] == []
    assert row["stage"] == Stage.EXECUTING.value
    assert row["tool_name"] == "executor"
    assert row["success"] == 1
    assert row["exit_code"] == 0
    assert row["failure_code"] is None
    task = get_task(sqlite_conn, "task-exec")
    assert task is not None
    assert task.last_known_head_commit is not None


def test_run_executor_raises_lease_error_before_spawning_subprocess(
    sqlite_conn: sqlite3.Connection,
    git_repo: Path,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    seed_task(
        sqlite_conn,
        task_id="task-stale-lease",
        created_at=now,
        state=TaskState.IMPLEMENTING,
        lease_branch="main",
    )
    redis_lease = Mock(unsafe=True)
    redis_lease.assert_readonly.return_value = False
    runner = ToolRunner(
        "task-stale-lease",
        _conn_factory(sqlite_conn),
        JournalWriter(tmp_path / "journal", FrozenClock(now)),
        redis_client=object(),
        settings=Settings(log_dir=tmp_path / "logs", executor_workspace_root=tmp_path / "workspaces"),
        repo_root=git_repo,
        redis_lease=redis_lease,
        fencing_token=9,
        clock=FrozenClock(now),
    )
    runner.setup_worktree(lease_branch="main")

    with pytest.raises(LeaseError, match="Lease lost for branch=main task=task-stale-lease token=9"):
        runner.run_executor({"allowed_files": ["src/task_relay/**"], "auto_allowed_patterns": ["tests/**"]})

    redis_lease.assert_readonly.assert_called_once_with("main", "task-stale-lease", 9)


def test_run_review_records_tool_call(
    sqlite_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    seed_task(sqlite_conn, task_id="task-review", created_at=now, state=TaskState.REVIEWING)
    reviewer = ReviewerAdapter(
        FakeTransport(
            [
                {
                    "payload": {
                        "decision": "pass",
                        "criteria": [
                            {
                                "status": "satisfied",
                                "evidence_refs": ["diff:1"],
                            }
                        ],
                        "policy_breaches": [],
                        "extra_files": [],
                    },
                    "tokens_in": 3,
                    "tokens_out": 5,
                }
            ]
        )
    )
    runner = ToolRunner(
        "task-review",
        _conn_factory(sqlite_conn),
        JournalWriter(tmp_path / "journal", FrozenClock(now)),
        redis_client=object(),
        settings=Settings(log_dir=tmp_path / "logs", executor_workspace_root=tmp_path / "workspaces"),
        repo_root=tmp_path,
        reviewer=reviewer,
        clock=FrozenClock(now),
    )
    plan = Plan(
        task_id="task-review",
        plan_rev=1,
        planner_version="planner-v2",
        plan_json=_valid_plan_json(),
        validator_score=100,
        validator_errors=0,
        approved_by=None,
        approved_at=None,
        approved_kind=None,
        created_at=now,
    )

    result = runner.run_review(plan, "HEAD~1..HEAD")

    row = sqlite_conn.execute(
        "SELECT stage, tool_name, success, tokens_in, tokens_out FROM tool_calls"
    ).fetchone()
    assert row is not None
    assert result.ok is True
    assert result.payload["decision"] == "pass"
    assert result.payload["unchecked_count"] == 0
    assert row["stage"] == Stage.REVIEWING.value
    assert row["tool_name"] == "reviewer"
    assert row["success"] == 1
    assert row["tokens_in"] == 3
    assert row["tokens_out"] == 5


def test_run_review_pushes_feature_branch_on_pass(
    sqlite_conn: sqlite3.Connection,
    git_repo: Path,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    remote_repo = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote_repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(git_repo), "remote", "add", "origin", str(remote_repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(git_repo), "push", "-u", "origin", "main"], check=True, capture_output=True)
    seed_task(
        sqlite_conn,
        task_id="task-review-push",
        created_at=now,
        state=TaskState.REVIEWING,
        lease_branch="main",
    )
    reviewer = ReviewerAdapter(
        FakeTransport(
            [
                {
                    "payload": {
                        "decision": "pass",
                        "criteria": [],
                        "policy_breaches": [],
                        "extra_files": [],
                    }
                }
            ]
        )
    )
    runner = ToolRunner(
        "task-review-push",
        _conn_factory(sqlite_conn),
        JournalWriter(tmp_path / "journal", FrozenClock(now)),
        redis_client=object(),
        settings=Settings(log_dir=tmp_path / "logs", executor_workspace_root=tmp_path / "workspaces"),
        repo_root=git_repo,
        reviewer=reviewer,
        clock=FrozenClock(now),
    )
    feature_branch, _ = runner.setup_worktree(lease_branch="main")
    plan = Plan(
        task_id="task-review-push",
        plan_rev=1,
        planner_version="planner-v2",
        plan_json=_valid_plan_json(),
        validator_score=100,
        validator_errors=0,
        approved_by=None,
        approved_at=None,
        approved_kind=None,
        created_at=now,
    )

    result = runner.run_review(plan, "HEAD~1..HEAD")

    remote_ref = subprocess.run(
        ["git", "--git-dir", str(remote_repo), "show-ref", "--verify", f"refs/heads/{feature_branch}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert result.ok is True
    assert remote_ref.endswith(f"refs/heads/{feature_branch}")


def test_cleanup_worktree_removes_worktree(
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

    runner.cleanup_worktree()

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


def _valid_plan_json() -> dict[str, object]:
    return {
        "goal": "Implement ToolRunner",
        "sub_tasks": ["wire subprocess orchestration"],
        "allowed_files": ["src/task_relay/**"],
        "auto_allowed_patterns": ["tests/**"],
        "acceptance_criteria": ["tests pass"],
        "forbidden_changes": ["no schema drift"],
        "risk_notes": ["subprocess timeout handling"],
    }
