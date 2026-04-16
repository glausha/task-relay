from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from task_relay.clock import Clock, SystemClock
from task_relay.config import Settings
from task_relay.db.queries import get_task, update_task_worktree
from task_relay.ids import new_event_id
from task_relay.journal.writer import JournalWriter
from task_relay.runner.adapters.base import AdapterOutput, TimeoutDecision
from task_relay.runner.worktree import create_worktree, remove_worktree, worktree_exists
from task_relay.types import AdapterContract, CanonicalEvent, JournalPosition, Plan, Source, Stage, TaskState


SIGTERM_GRACE_SECONDS = 15


def decide_timeout_retry(
    *,
    stage: Stage,
    contract: AdapterContract,
    attempt_count: int,
) -> TimeoutDecision:
    """
    detailed-design §8.1 timeout / oom_killed classification.
    """
    if stage == Stage.EXECUTING:
        return TimeoutDecision.GIVE_UP_HR
    if not contract.supports_request_id:
        return TimeoutDecision.GIVE_UP_HR
    if attempt_count >= 1:
        return TimeoutDecision.GIVE_UP_HR
    return TimeoutDecision.RETRY


class ToolRunner:
    def __init__(
        self,
        task_id: str,
        conn_factory: Callable[[], sqlite3.Connection],
        journal_writer: JournalWriter,
        redis_client: Any,
        settings: Settings,
        repo_root: Path,
        clock: Clock = SystemClock(),
    ) -> None:
        self._task_id = task_id
        self._conn_factory = conn_factory
        self._journal_writer = journal_writer
        self._redis_client = redis_client
        self._settings = settings
        self._repo_root = repo_root
        self._clock = clock

    def run_planning(self, plan_input: dict[str, Any]) -> AdapterOutput:
        raise NotImplementedError("Phase 2: tool_runner subprocess")

    def run_executor(self, plan_json: dict[str, Any]) -> AdapterOutput:
        raise NotImplementedError("Phase 2: tool_runner subprocess")

    def run_review(self, plan: Plan, diff_ref: str) -> AdapterOutput:
        raise NotImplementedError("Phase 2: tool_runner subprocess")

    def observe_state_change(self, state: TaskState) -> None:
        conn = self._conn_factory()
        try:
            task = get_task(conn, self._task_id)
        finally:
            conn.close()
        if task is None:
            return
        if state is TaskState.IMPLEMENTING:
            if task.lease_branch is None:
                return
            if task.worktree_path and worktree_exists(Path(task.worktree_path)):
                return
            self.setup_worktree(lease_branch=task.lease_branch)
            return
        if state in {TaskState.DONE, TaskState.CANCELLED}:
            self.cleanup_worktree()

    def setup_worktree(self, *, lease_branch: str) -> tuple[str, Path]:
        """dispatch 時に Router から呼ばれる。worktree を作って task metadata を更新。"""
        feature_branch, worktree_path = create_worktree(
            repo_root=self._repo_root,
            workspace_root=self._settings.executor_workspace_root,
            task_id=self._task_id,
            lease_branch=lease_branch,
        )
        conn = self._conn_factory()
        try:
            update_task_worktree(
                conn,
                self._task_id,
                lease_branch=lease_branch,
                feature_branch=feature_branch,
                worktree_path=str(worktree_path),
            )
            conn.commit()
        finally:
            conn.close()
        return feature_branch, worktree_path

    def cleanup_worktree(self) -> None:
        """done/cancelled 時に呼ばれる。worktree と feature_branch を削除。"""
        conn = self._conn_factory()
        try:
            task = get_task(conn, self._task_id)
        finally:
            conn.close()
        if task and task.worktree_path:
            remove_worktree(
                repo_root=self._repo_root,
                worktree_path=Path(task.worktree_path),
                feature_branch=task.feature_branch,
            )

    def append_internal_event(self, event_type: str, payload: dict[str, Any]) -> JournalPosition:
        event_id = new_event_id()
        event = CanonicalEvent(
            event_id=event_id,
            source=Source.INTERNAL,
            delivery_id=event_id,
            event_type=event_type,
            payload={"task_id": self._task_id, **payload},
            received_at=self._clock.now(),
            request_id=None,
        )
        return self._journal_writer.append(event)


def _terminate_subprocess(
    proc: subprocess.Popen[Any],
    grace_seconds: int = SIGTERM_GRACE_SECONDS,
) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
