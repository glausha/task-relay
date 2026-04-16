"""Task dispatcher: detailed-design §7.1."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from task_relay.branch_lease.redis_lease import LeaseHandle, RedisLease
from task_relay.breaker.circuit_breaker import CircuitBreaker
from task_relay.clock import Clock, SystemClock
from task_relay.config import Settings
from task_relay.db import queries
from task_relay.errors import FAILURE_CLASS, FailureClass, FailureCode, TimeoutTransportError
from task_relay.ids import new_event_id
from task_relay.journal.writer import JournalWriter
from task_relay.runner.adapters.base import AdapterBase, AdapterOutput
from task_relay.runner.retry_system_handler import RetrySystemHandler
from task_relay.runner.tool_runner import ToolRunner
from task_relay.runner.unlock_handler import UnlockHandler
from task_relay.types import BranchWaiterStatus, CanonicalEvent, InboxEvent, Plan, Source, Task, TaskState

if TYPE_CHECKING:
    from task_relay.router.router import RouterResult


class TaskDispatcher:
    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        journal_writer: JournalWriter,
        settings: Settings,
        redis_client: Any,
        planner: AdapterBase,
        reviewer: AdapterBase,
        *,
        clock: Clock = SystemClock(),
    ) -> None:
        self._conn_factory = conn_factory
        self._journal_writer = journal_writer
        self._settings = settings
        self._redis_client = redis_client
        self._planner = planner
        self._reviewer = reviewer
        self._clock = clock
        self._redis_lease = RedisLease(redis_client, settings, clock)
        self._breaker = CircuitBreaker(
            window_seconds=settings.breaker_window_seconds,
            fatal_threshold=settings.breaker_fatal_threshold,
            clock=clock,
            conn_factory=conn_factory,
        )
        self._unlock_handler = UnlockHandler(conn_factory, self._redis_lease, journal_writer, clock=clock)
        self._retry_system_handler = RetrySystemHandler(
            self._breaker,
            journal_writer,
            conn_factory=conn_factory,
            redis_client=redis_client,
            clock=clock,
        )
        self._lease_handles: dict[str, LeaseHandle] = {}
        self._last_dispatched_task_id: str | None = None
        conn = self._conn_factory()
        try:
            self._breaker.rebuild_from_events(conn)
        finally:
            conn.close()

    def step(self) -> int:
        """1 件 dispatch すれば 1、なければ 0。"""
        self._last_dispatched_task_id = None
        if self._breaker.open_codes(self._clock.now()):
            return 0

        conn = self._conn_factory()
        try:
            candidate = self._select_dispatch_candidate(conn)
            if candidate is None:
                return 0
            branch, task_id = candidate
            fencing_token = queries.next_branch_token(conn, branch)
            lease_handle = self._redis_lease.acquire(
                branch=branch,
                task_id=task_id,
                fencing_token=fencing_token,
                ttl_sec=self._settings.lease_ttl_seconds,
            )
            if lease_handle is None:
                return 0
            self._append_internal_event(
                "internal.dispatch_attempt",
                {
                    "task_id": task_id,
                    "lease_acquired": True,
                    "lease_branch": branch,
                    "fencing_token": fencing_token,
                },
            )
            # WHY: mark the waiter leased so repeated polling does not emit duplicate dispatches.
            queries.update_waiter_status(conn, branch, task_id, BranchWaiterStatus.LEASED)
            conn.commit()
        finally:
            conn.close()

        self._lease_handles[task_id] = lease_handle
        self._last_dispatched_task_id = task_id
        return 1

    def run_task(self, task_id: str) -> None:
        """1 task の current runnable state を実行する。"""
        conn = self._conn_factory()
        try:
            task = queries.get_task(conn, task_id)
        finally:
            conn.close()
        if task is None:
            return

        runner = self._tool_runner(task_id)
        if task.state == TaskState.PLANNING:
            self._run_planning_stage(task, runner)
            return
        if task.state == TaskState.IMPLEMENTING:
            runner.observe_state_change(TaskState.IMPLEMENTING)
            self._run_implementing_stage(task, runner)
            return
        if task.state == TaskState.REVIEWING:
            self._run_reviewing_stage(task, runner)

    def run_forever(self, poll_interval: float = 1.0) -> None:
        """main loop: step() → run_task() → sleep の繰り返し。"""
        while True:
            self.step()
            task_id = self._last_dispatched_task_id or self._next_runnable_task_id()
            if task_id is not None:
                self.run_task(task_id)
            time.sleep(poll_interval)

    def handle_router_post_apply(self, event: InboxEvent, result: RouterResult) -> None:
        if result.skipped:
            return
        if event.event_type == "/unlock":
            branch = event.payload.get("branch")
            if branch is not None:
                self._unlock_handler.handle_unlock(str(branch))
            return
        if event.event_type == "/retry-system":
            stage = event.payload.get("stage")
            self._retry_system_handler.handle_retry_system(None if stage is None else str(stage))

    def _tool_runner(self, task_id: str) -> ToolRunner:
        return ToolRunner(
            task_id=task_id,
            conn_factory=self._conn_factory,
            journal_writer=self._journal_writer,
            redis_client=self._redis_client,
            settings=self._settings,
            clock=self._clock,
            repo_root=self._settings.executor_workspace_root.parent,
            planner=self._planner,
            reviewer=self._reviewer,
            lease_handle=self._lease_handles.get(task_id),
            redis_lease=self._redis_lease,
        )

    def _run_planning_stage(self, task: Task, runner: ToolRunner) -> None:
        try:
            result = runner.run_planning(self._planning_payload(task))
        except TimeoutTransportError:
            return
        if not result.ok:
            self._handle_stage_failure(
                task_id=task.task_id,
                result=result,
                default_event_type="internal.validator_over",
            )
            return

        created_at = self._clock.now()
        plan = self._insert_plan(task.task_id, result, created_at)
        runner.append_internal_event(
            "internal.plan_ready",
            {
                "plan_rev": plan.plan_rev,
                "validator_score": plan.validator_score,
                "validator_errors": plan.validator_errors,
            },
        )

    def _run_implementing_stage(self, task: Task, runner: ToolRunner) -> None:
        plan = self._require_latest_plan(task.task_id)
        result = runner.run_executor(plan.plan_json)
        if not result.ok:
            self._handle_executor_failure(runner, result)
            return
        payload = dict(result.payload)
        payload["plan_rev"] = plan.plan_rev
        runner.append_internal_event("internal.executor_finished", payload)

    def _run_reviewing_stage(self, task: Task, runner: ToolRunner) -> None:
        plan = self._require_latest_plan(task.task_id)
        try:
            result = runner.run_review(plan, self._diff_ref(task))
        except TimeoutTransportError:
            runner.append_internal_event("internal.reviewer_timeout", {"failure_code": FailureCode.TIMEOUT.value})
            return
        if not result.ok:
            self._handle_stage_failure(
                task_id=task.task_id,
                result=result,
                default_event_type="internal.reviewer_human_review",
            )
            return

        decision = str(result.payload.get("decision", "human_review_required"))
        event_type = {
            "pass": "internal.reviewer_pass",
            "fail": "internal.reviewer_fail",
            "human_review_required": "internal.reviewer_human_review",
        }.get(decision, "internal.reviewer_human_review")
        runner.append_internal_event(event_type, result.payload)

    def _handle_executor_failure(self, runner: ToolRunner, result: AdapterOutput) -> None:
        failure_code = result.failure_code
        if failure_code is not None and FAILURE_CLASS[failure_code] == FailureClass.FATAL:
            self._breaker.record(failure_code, self._clock.now())
            runner.append_internal_event("internal.infra_fatal", {"failure_code": failure_code.value})
            return
        payload = dict(result.payload)
        payload.setdefault("exit_code", 1)
        if failure_code is not None:
            payload["failure_code"] = failure_code.value
        runner.append_internal_event("internal.executor_finished", payload)

    def _handle_stage_failure(
        self,
        *,
        task_id: str,
        result: AdapterOutput,
        default_event_type: str,
    ) -> None:
        runner = self._tool_runner(task_id)
        failure_code = result.failure_code
        if failure_code is not None and FAILURE_CLASS[failure_code] == FailureClass.FATAL:
            self._breaker.record(failure_code, self._clock.now())
            runner.append_internal_event("internal.infra_fatal", {"failure_code": failure_code.value})
            return
        payload = {} if failure_code is None else {"failure_code": failure_code.value}
        runner.append_internal_event(default_event_type, payload)

    def _insert_plan(self, task_id: str, result: AdapterOutput, created_at: datetime) -> Plan:
        conn = self._conn_factory()
        try:
            latest = queries.get_latest_plan(conn, task_id)
            plan_rev = 1 if latest is None else latest.plan_rev + 1
            plan = Plan(
                task_id=task_id,
                plan_rev=plan_rev,
                planner_version=f"{self._planner.contract.name}-{self._planner.contract.version}",
                plan_json=result.payload,
                validator_score=int(result.payload.get("validator_score", 0)),
                validator_errors=int(result.payload.get("validator_errors", 0)),
                approved_by=None,
                approved_at=None,
                approved_kind=None,
                created_at=created_at,
            )
            queries.insert_plan(conn, plan)
            conn.commit()
            return plan
        finally:
            conn.close()

    def _require_latest_plan(self, task_id: str) -> Plan:
        conn = self._conn_factory()
        try:
            plan = queries.get_latest_plan(conn, task_id)
        finally:
            conn.close()
        if plan is None:
            raise RuntimeError(f"latest plan not found for task {task_id}")
        return plan

    def _planning_payload(self, task: Task) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "source_issue_id": task.source_issue_id,
            "requested_by": task.requested_by,
            "critical": task.critical,
            "lease_branch": task.lease_branch,
            "manual_gate_required": task.manual_gate_required,
        }

    def _diff_ref(self, task: Task) -> str:
        if task.last_known_head_commit:
            return f"{task.last_known_head_commit}..HEAD"
        return "HEAD~1..HEAD"

    def _next_runnable_task_id(self) -> str | None:
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """
                SELECT task_id
                FROM tasks
                WHERE state IN (?, ?, ?)
                ORDER BY updated_at ASC, task_id ASC
                LIMIT 1
                """,
                (
                    TaskState.PLANNING.value,
                    TaskState.IMPLEMENTING.value,
                    TaskState.REVIEWING.value,
                ),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return str(row["task_id"])

    def _select_dispatch_candidate(self, conn: sqlite3.Connection) -> tuple[str, str] | None:
        row = conn.execute(
            """
            SELECT bw.branch, bw.task_id
            FROM branch_waiters AS bw
            JOIN tasks AS t
              ON t.task_id = bw.task_id
            WHERE bw.status = ?
              AND t.state = ?
              AND bw.queue_order = (
                  SELECT MIN(head.queue_order)
                  FROM branch_waiters AS head
                  WHERE head.branch = bw.branch
                    AND head.status = ?
              )
            ORDER BY bw.branch ASC, bw.queue_order ASC, bw.task_id ASC
            LIMIT 1
            """,
            (
                BranchWaiterStatus.QUEUED.value,
                TaskState.PLAN_APPROVED.value,
                BranchWaiterStatus.QUEUED.value,
            ),
        ).fetchone()
        if row is None:
            return None
        return str(row["branch"]), str(row["task_id"])

    def _append_internal_event(self, event_type: str, payload: dict[str, Any]) -> None:
        received_at = self._clock.now()
        event_id = new_event_id()
        event = CanonicalEvent(
            event_id=event_id,
            source=Source.INTERNAL,
            delivery_id=event_id,
            event_type=event_type,
            payload=payload,
            received_at=received_at,
            request_id=None,
        )
        self._journal_writer.append(event)
