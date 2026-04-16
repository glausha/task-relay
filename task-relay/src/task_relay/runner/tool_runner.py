from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from task_relay.branch_lease.redis_lease import LeaseHandle, RedisLease
from task_relay.clock import Clock, SystemClock
from task_relay.config import Settings
from task_relay.db.queries import get_task, insert_tool_call, update_task_worktree, update_tool_call_end
from task_relay.errors import FailureCode, TimeoutTransportError
from task_relay.ids import new_call_id, new_event_id, new_request_id
from task_relay.journal.writer import JournalWriter
from task_relay.runner.adapters.base import AdapterOutput, TimeoutDecision
from task_relay.runner.adapters.executor import check_file_scope
from task_relay.runner.adapters.planner import PlannerAdapter
from task_relay.runner.adapters.reviewer import ReviewerAdapter
from task_relay.runner.git_safety import (
    assert_lease_before_mutate,
    push_feature_branch,
    update_head_commit,
)
from task_relay.runner.log_writer import LogWriter
from task_relay.runner.worktree import create_worktree, remove_worktree, worktree_exists
from task_relay.types import (
    AdapterContract,
    CanonicalEvent,
    JournalPosition,
    Plan,
    Source,
    Stage,
    Task,
    TaskState,
    ToolCallRecord,
)


SIGTERM_GRACE_SECONDS = 15
_SUBPROCESS_TIMEOUT_FACTOR = 40


@dataclass
class _ExecutionControl:
    termination_reason: str | None = None
    lease_handle: LeaseHandle | None = None


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
        planner: PlannerAdapter | None = None,
        reviewer: ReviewerAdapter | None = None,
        lease_handle: LeaseHandle | None = None,
        redis_lease: RedisLease | None = None,
        fencing_token: int | None = None,
        clock: Clock = SystemClock(),
    ) -> None:
        self._task_id = task_id
        self._conn_factory = conn_factory
        self._journal_writer = journal_writer
        self._redis_client = redis_client
        self._settings = settings
        self._repo_root = repo_root
        self._clock = clock
        self._planner = planner
        self._reviewer = reviewer
        self._lease_handle = lease_handle
        self._redis_lease = redis_lease or self._build_redis_lease(redis_client)
        self._fencing_token = lease_handle.fencing_token if fencing_token is None and lease_handle else fencing_token

    def run_planning(self, plan_input: dict[str, Any]) -> AdapterOutput:
        planner = self._require_planner()
        request_payload = {
            "task_goal": plan_input["goal"],
            "repo_context": plan_input.get("repo_context", ""),
        }
        if "repo_summary" in plan_input:
            request_payload["repo_summary"] = plan_input["repo_summary"]
        return self._run_in_process_stage(
            stage=Stage.PLANNING,
            tool_name=planner.contract.name,
            request_payload=request_payload,
            call_adapter=planner.call,
            timeout_event_type="internal.planner_timeout",
        )

    def run_executor(self, plan_json: dict[str, Any]) -> AdapterOutput:
        task = self._require_task()
        worktree_path = self._require_worktree_path(task)
        # WHY: git writes must fail fast if this worker no longer owns the branch lease.
        self._assert_lease_before_mutate(task.lease_branch)
        call_id = new_call_id()
        started = self._clock.now()
        log_writer = LogWriter(self._settings.log_dir, self._task_id, Stage.EXECUTING, call_id, started)
        self._insert_tool_call_start(call_id=call_id, stage=Stage.EXECUTING, tool_name="executor", started_at=started)
        log_writer.write_line({"event": "executor_input", "payload": plan_json})
        request_id = new_request_id()
        child_payload = {
            "executor_child_protocol": "v1",
            "request_id": request_id,
            "task_id": self._task_id,
            "plan_json": plan_json,
            "allowed_files": plan_json.get("allowed_files", []),
            "auto_allowed_patterns": plan_json.get("auto_allowed_patterns", []),
            "timeout": self._settings.executor_timeout,
            "worktree_path": str(worktree_path),
            "_mock_response": plan_json.get("_mock_response"),
        }
        log_writer.write_line({"event": "executor_child_payload", "payload": child_payload})

        env = os.environ.copy()
        current_src_root = Path(__file__).resolve().parents[2]
        pythonpath_entries = [str(current_src_root)]
        worktree_src_root = worktree_path / "src"
        if worktree_src_root != current_src_root:
            pythonpath_entries.append(str(worktree_src_root))
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(
            [*pythonpath_entries, *([] if not existing_pythonpath else [existing_pythonpath])]
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "task_relay.runner.executor_child"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=worktree_path,
            env=env,
        )

        stdin = proc.stdin
        if stdin is None:
            raise RuntimeError("executor child stdin pipe is unavailable")
        stdin.write(json.dumps(child_payload).encode("utf-8"))
        stdin.close()

        control = _ExecutionControl(lease_handle=self._lease_handle)
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_thread = threading.Thread(
            target=self._stream_reader_loop,
            args=(proc.stdout, stdout_buffer),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._stream_reader_loop,
            args=(proc.stderr, stderr_buffer),
            daemon=True,
        )
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(proc, control),
            daemon=True,
        )
        state_thread = threading.Thread(
            target=self._state_observer_loop,
            args=(proc, control),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        heartbeat_thread.start()
        state_thread.start()

        timed_out = False
        try:
            proc.wait(timeout=self._settings.subprocess_sigterm_grace_seconds * _SUBPROCESS_TIMEOUT_FACTOR)
        except subprocess.TimeoutExpired:
            timed_out = True
            control.termination_reason = "timeout"
            _terminate_subprocess(proc, self._settings.subprocess_sigterm_grace_seconds)

        stdout_thread.join()
        stderr_thread.join()
        stdout_data = bytes(stdout_buffer)
        stderr_data = bytes(stderr_buffer)
        stderr_text = stderr_data.decode("utf-8", errors="replace") or None
        stdout_text = stdout_data.decode("utf-8", errors="replace") or None
        if stdout_text:
            log_writer.write_line({"event": "executor_stdout", "text": stdout_text})
        if stderr_text:
            log_writer.write_line({"event": "executor_stderr", "text": stderr_text})

        if timed_out:
            log_writer.write_line({"event": "executor_timeout"})
            result = AdapterOutput(
                ok=False,
                payload={},
                failure_code=FailureCode.TIMEOUT,
                tokens_in=None,
                tokens_out=None,
                raw_text=stderr_text or stdout_text,
            )
            self._record_call_end(
                call_id,
                started,
                False,
                exit_code=proc.returncode,
                failure_code=result.failure_code,
                log_writer=log_writer,
            )
            return result

        if proc.returncode != 0:
            failure_code = self._classify_executor_failure(control)
            log_writer.write_line(
                {
                    "event": "executor_failed",
                    "returncode": proc.returncode,
                    "failure_code": failure_code.value,
                }
            )
            result = AdapterOutput(
                ok=False,
                payload={},
                failure_code=failure_code,
                tokens_in=None,
                tokens_out=None,
                raw_text=stderr_text or stdout_text,
            )
            self._record_call_end(
                call_id,
                started,
                False,
                exit_code=proc.returncode,
                failure_code=failure_code,
                log_writer=log_writer,
            )
            return result

        try:
            child_result = json.loads(stdout_text or "{}")
        except json.JSONDecodeError:
            log_writer.write_line({"event": "executor_invalid_json", "stdout": stdout_text})
            result = AdapterOutput(
                ok=False,
                payload={},
                failure_code=FailureCode.ADAPTER_PARSE_ERROR,
                tokens_in=None,
                tokens_out=None,
                raw_text=stdout_text,
            )
            self._record_call_end(
                call_id,
                started,
                False,
                exit_code=proc.returncode,
                failure_code=result.failure_code,
                log_writer=log_writer,
            )
            return result
        child_exit_code = child_result.get("exit_code")
        if not isinstance(child_exit_code, int):
            child_exit_code = proc.returncode
        if child_exit_code != 0:
            failure_code = self._classify_executor_failure(control)
            log_writer.write_line(
                {
                    "event": "executor_child_failed",
                    "exit_code": child_exit_code,
                    "failure_code": failure_code.value,
                }
            )
            result = AdapterOutput(
                ok=False,
                payload={},
                failure_code=failure_code,
                tokens_in=None,
                tokens_out=None,
                raw_text=stderr_text or stdout_text,
            )
            self._record_call_end(
                call_id,
                started,
                False,
                exit_code=child_exit_code,
                failure_code=failure_code,
                log_writer=log_writer,
            )
            return result

        conn = self._conn_factory()
        try:
            # WHY: reviewer diffs must anchor to the post-mutate HEAD seen by the successful executor run.
            update_head_commit(conn, self._task_id, worktree_path, self._clock.now())
            conn.commit()
        finally:
            conn.close()

        result = self._executor_output_from_child(plan_json=plan_json, response=child_result)
        log_writer.write_line({"event": "executor_result", "result": result.payload if result.ok else child_result})
        self._record_call_end(
            call_id,
            started,
            result.ok,
            exit_code=child_exit_code,
            failure_code=result.failure_code,
            log_writer=log_writer,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
        )
        return result

    def run_review(self, plan: Plan, diff_ref: str) -> AdapterOutput:
        reviewer = self._require_reviewer()
        review_input = {
            "task_id": self._task_id,
            "plan_rev": plan.plan_rev,
            "plan_json": plan.plan_json,
            "diff_ref": diff_ref,
        }
        result = self._run_in_process_stage(
            stage=Stage.REVIEWING,
            tool_name=reviewer.contract.name,
            request_payload=review_input,
            call_adapter=reviewer.call,
            timeout_event_type=None,
        )
        if result.ok and result.payload.get("decision") == "pass":
            self._push_feature_branch_after_review()
        return result

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

    def _build_redis_lease(self, redis_client: Any) -> RedisLease | None:
        required_attrs = ("script_load", "evalsha", "time", "pttl")
        if all(hasattr(redis_client, attr) for attr in required_attrs):
            return RedisLease(redis_client, self._settings, self._clock)
        return None

    def _assert_lease_before_mutate(self, branch: str | None) -> None:
        if self._redis_lease is None or branch is None or self._fencing_token is None:
            return
        assert_lease_before_mutate(
            self._redis_lease,
            branch=branch,
            task_id=self._task_id,
            fencing_token=self._fencing_token,
        )

    def _push_feature_branch_after_review(self) -> None:
        task = self._require_task()
        if task.worktree_path is None or task.feature_branch is None:
            return
        push_feature_branch(Path(task.worktree_path), task.feature_branch)

    def _require_planner(self) -> PlannerAdapter:
        if self._planner is None:
            raise RuntimeError("planner adapter is not configured")
        return self._planner

    def _require_reviewer(self) -> ReviewerAdapter:
        if self._reviewer is None:
            raise RuntimeError("reviewer adapter is not configured")
        return self._reviewer

    def _run_in_process_stage(
        self,
        *,
        stage: Stage,
        tool_name: str,
        request_payload: dict[str, Any],
        call_adapter: Callable[..., AdapterOutput],
        timeout_event_type: str | None,
    ) -> AdapterOutput:
        call_id = new_call_id()
        request_id = new_request_id()
        started = self._clock.now()
        log_writer = LogWriter(self._settings.log_dir, self._task_id, stage, call_id, started)
        self._insert_tool_call_start(call_id=call_id, stage=stage, tool_name=tool_name, started_at=started)
        log_writer.write_line({"event": "request", "request_id": request_id, "payload": request_payload})

        attempt_count = 0
        while True:
            try:
                result = call_adapter(request_id=request_id, payload=request_payload)
            except TimeoutTransportError:
                decision = decide_timeout_retry(
                    stage=stage,
                    contract=self._contract_for_tool(tool_name),
                    attempt_count=attempt_count,
                )
                log_writer.write_line(
                    {"event": "timeout", "request_id": request_id, "decision": decision.value, "attempt": attempt_count}
                )
                if decision is TimeoutDecision.RETRY:
                    attempt_count += 1
                    continue
                if timeout_event_type is not None:
                    self.append_internal_event(timeout_event_type, {"failure_code": FailureCode.TIMEOUT.value})
                self._record_call_end(
                    call_id,
                    started,
                    False,
                    failure_code=FailureCode.TIMEOUT,
                    log_writer=log_writer,
                )
                raise

            log_writer.write_line(
                {
                    "event": "response",
                    "ok": result.ok,
                    "payload": result.payload,
                    "failure_code": None if result.failure_code is None else result.failure_code.value,
                }
            )
            self._record_call_end(
                call_id,
                started,
                result.ok,
                failure_code=result.failure_code,
                log_writer=log_writer,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
            )
            return result

    def _contract_for_tool(self, tool_name: str) -> AdapterContract:
        if self._planner is not None and self._planner.contract.name == tool_name:
            return self._planner.contract
        if self._reviewer is not None and self._reviewer.contract.name == tool_name:
            return self._reviewer.contract
        return AdapterContract(name=tool_name, version="v1", supports_request_id=False)

    def _insert_tool_call_start(
        self,
        *,
        call_id: str,
        stage: Stage,
        tool_name: str,
        started_at: datetime,
    ) -> None:
        conn = self._conn_factory()
        try:
            insert_tool_call(
                conn,
                ToolCallRecord(
                    call_id=call_id,
                    task_id=self._task_id,
                    stage=stage,
                    tool_name=tool_name,
                    started_at=started_at,
                    ended_at=None,
                    duration_ms=None,
                    success=None,
                    exit_code=None,
                    failure_code=None,
                    log_path=None,
                    log_sha256=None,
                    log_bytes=None,
                    tokens_in=None,
                    tokens_out=None,
                ),
            )
        finally:
            conn.close()

    def _record_call_end(
        self,
        call_id: str,
        started: datetime,
        success: bool,
        *,
        exit_code: int | None = None,
        failure_code: FailureCode | None = None,
        log_writer: LogWriter | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        ended = self._clock.now()
        duration_ms = int((ended - started).total_seconds() * 1000)
        log_path: str | None = None
        log_sha256: str | None = None
        log_bytes: int | None = None
        if log_writer is not None:
            path, sha256, size = log_writer.close()
            log_path = str(path)
            log_sha256 = sha256
            log_bytes = size
        conn = self._conn_factory()
        try:
            update_tool_call_end(
                conn,
                call_id,
                ended,
                duration_ms,
                success,
                exit_code,
                None if failure_code is None else failure_code.value,
                log_path,
                log_sha256,
                log_bytes,
                tokens_in,
                tokens_out,
            )
        finally:
            conn.close()

    def _require_task(self) -> Task:
        conn = self._conn_factory()
        try:
            task = get_task(conn, self._task_id)
        finally:
            conn.close()
        if task is None:
            raise RuntimeError(f"task {self._task_id} is not configured")
        return task

    def _require_worktree_path(self, task: Task | None = None) -> Path:
        if task is None:
            task = self._require_task()
        if task.worktree_path is None:
            raise RuntimeError("executor worktree is not configured")
        return Path(task.worktree_path)

    def _executor_output_from_child(
        self,
        *,
        plan_json: dict[str, Any],
        response: dict[str, Any],
    ) -> AdapterOutput:
        payload = response.get("payload")
        if payload is None:
            payload = {
                key: value
                for key, value in response.items()
                if key not in {"tokens_in", "tokens_out", "raw_text"}
            }
        if not isinstance(payload, dict):
            return AdapterOutput(
                ok=False,
                payload={},
                failure_code=FailureCode.ADAPTER_PARSE_ERROR,
                tokens_in=None,
                tokens_out=None,
                raw_text=response.get("raw_text") if isinstance(response.get("raw_text"), str) else None,
            )
        in_scope, out_of_scope = check_file_scope(
            payload.get("changed_files", []),
            plan_json.get("allowed_files", []),
            plan_json.get("auto_allowed_patterns", []),
        )
        return AdapterOutput(
            ok=True,
            payload={**payload, "in_scope_files": in_scope, "out_of_scope_files": out_of_scope},
            failure_code=None,
            tokens_in=response.get("tokens_in") if isinstance(response.get("tokens_in"), int) else None,
            tokens_out=response.get("tokens_out") if isinstance(response.get("tokens_out"), int) else None,
            raw_text=response.get("raw_text") if isinstance(response.get("raw_text"), str) else None,
        )

    def _heartbeat_loop(self, proc: subprocess.Popen[Any], control: _ExecutionControl) -> None:
        if self._redis_lease is None or control.lease_handle is None:
            return
        consecutive_failures = 0
        while proc.poll() is None:
            time.sleep(self._settings.lease_renew_interval_seconds)
            if proc.poll() is not None:
                return
            renewed = self._redis_lease.renew(control.lease_handle)
            if renewed is not None:
                consecutive_failures = 0
                control.lease_handle = renewed
                continue
            consecutive_failures += 1
            if consecutive_failures < 2:
                continue
            control.termination_reason = "lease_lost"
            _terminate_subprocess(proc, self._settings.subprocess_sigterm_grace_seconds)
            self.append_internal_event("internal.lease_lost", {"failure_code": FailureCode.SYSTEM_DEGRADED.value})
            return

    def _state_observer_loop(self, proc: subprocess.Popen[Any], control: _ExecutionControl) -> None:
        watched_states = {
            TaskState.CANCELLED,
            TaskState.SYSTEM_DEGRADED,
            TaskState.HUMAN_REVIEW_REQUIRED,
        }
        while proc.poll() is None:
            time.sleep(1)
            if proc.poll() is not None:
                return
            conn = self._conn_factory()
            try:
                task = get_task(conn, self._task_id)
            finally:
                conn.close()
            if task is None or task.state not in watched_states:
                continue
            control.termination_reason = task.state.value
            _terminate_subprocess(proc, self._settings.subprocess_sigterm_grace_seconds)
            return

    def _stream_reader_loop(
        self,
        stream: Any,
        buffer: bytearray,
    ) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
        finally:
            stream.close()

    def _classify_executor_failure(self, control: _ExecutionControl) -> FailureCode:
        if control.termination_reason == "lease_lost":
            return FailureCode.SYSTEM_DEGRADED
        if control.termination_reason == TaskState.SYSTEM_DEGRADED.value:
            return FailureCode.SYSTEM_DEGRADED
        return FailureCode.TOOL_INTERNAL_ERROR


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
