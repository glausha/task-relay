from __future__ import annotations

import json
import sqlite3
import time
from typing import Any
from collections.abc import Callable
from pathlib import Path

import click
from aiohttp import web

from .breaker.circuit_breaker import CircuitBreaker
from .config import Settings
from .db import queries
from .db.connection import connect
from .db.migrations import apply_schema
from .ingester.journal_ingester import JournalIngester
from .ingress.cli_source import build_cli_event
from .ingress.discord_bot import TaskRelayBot
from .ingress.discord_gateway import DiscordIngress
from .ingress.forgejo_server import ForgejoWebhookServer
from .ingress.forgejo_webhook import canonicalize, verify_signature
from .journal.reader import JournalReader
from .journal.writer import JournalWriter
from .logging_conf import setup_logging
from .projection import LoggingSink
from .projection.rebuild import rebuild_for_task
from .projection.worker import ProjectionWorker
from .rate.windows import should_stop_new_tasks
from .retention.journal_retention import JournalRetention
from .retention.log_retention import LogRetention
from .router.router import Router
from .types import Stream, TaskState


def _open_conn(settings: Settings) -> sqlite3.Connection:
    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return connect(settings.sqlite_path)


def _open_writer(settings: Settings) -> JournalWriter:
    settings.journal_dir.mkdir(parents=True, exist_ok=True)
    return JournalWriter(settings.journal_dir)


def _warm_circuit_breaker(
    conn: sqlite3.Connection,
    *,
    conn_factory: Callable[[], sqlite3.Connection] | None = None,
) -> CircuitBreaker:
    breaker = CircuitBreaker(conn_factory=conn_factory)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'system_events'").fetchone() is None:
        return breaker
    # WHY: restart must not implicitly clear breaker history inside the active window.
    breaker.rebuild_from_events(conn)
    return breaker


def _append_cli_command(
    settings: Settings,
    *,
    event_type: str,
    task_id: str | None,
    actor: str,
    payload: dict[str, Any] | None = None,
) -> str:
    try:
        writer = _open_writer(settings)
        try:
            event = build_cli_event(
                event_type=event_type,
                task_id=task_id,
                actor=actor,
                payload=payload,
            )
            writer.append(event)
        finally:
            writer.close()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    return event.request_id or ""


def _scope_label(
    *,
    in_progress: int,
    waiting_human: int,
    global_degraded: int,
    rate_protected: bool,
) -> str:
    if global_degraded > 0:
        return "全体障害"
    if rate_protected:
        return "全体保護中"
    if waiting_human > 0:
        return "局所障害"
    if in_progress > 0:
        return "進行中"
    return "待機中"


@click.group(name="task-relay")
@click.option("--config", type=click.Path(path_type=Path, dir_okay=False), default=None)
@click.option("-v", "--verbose", is_flag=True, default=False)
@click.pass_context
def cli(ctx: click.Context, config: Path | None, verbose: bool) -> None:
    _ = config
    setup_logging("DEBUG" if verbose else "INFO")
    ctx.obj = Settings()


@cli.command("migrate")
@click.pass_obj
def migrate(settings: Settings) -> None:
    conn = _open_conn(settings)
    try:
        apply_schema(conn)
    finally:
        conn.close()
    click.echo(f"Schema applied: {settings.sqlite_path}")


@cli.command("ingress-forgejo")
@click.option("--serve", is_flag=True, default=False)
@click.option("--host", default=None)
@click.option("--port", default=None, type=int)
@click.option("--body", type=click.Path(path_type=Path, exists=True, dir_okay=False), default=None)
@click.option("--event", "event_name", default=None)
@click.option("--delivery-id", default=None)
@click.option("--signature", default=None)
@click.pass_obj
def ingress_forgejo(
    settings: Settings,
    serve: bool,
    host: str,
    port: int,
    body: Path | None,
    event_name: str | None,
    delivery_id: str | None,
    signature: str | None,
) -> None:
    listen_host = host or settings.forgejo_webhook_host
    listen_port = port or settings.forgejo_webhook_port
    if serve:
        if body is not None or event_name is not None or delivery_id is not None or signature is not None:
            raise click.ClickException("--serve cannot be combined with single-ingest options")
        writer = _open_writer(settings)
        server = ForgejoWebhookServer(
            writer,
            settings.forgejo_webhook_secret.get_secret_value().encode("utf-8"),
            host=listen_host,
            port=listen_port,
        )
        web.run_app(server.create_app(), host=listen_host, port=listen_port)
        return
    click.echo("ingress-forgejo single-ingest mode")
    if body is None:
        return
    if event_name is None or delivery_id is None or signature is None:
        raise click.ClickException("--body requires --event, --delivery-id, and --signature")
    body_bytes = body.read_bytes()
    secret = settings.forgejo_webhook_secret.get_secret_value().encode("utf-8")
    if not verify_signature(body_bytes, signature, secret):
        raise click.ClickException("forgejo webhook signature verification failed")
    canonical = canonicalize(event_name, delivery_id, json.loads(body_bytes.decode("utf-8")))
    if canonical is None:
        raise click.ClickException("forgejo webhook event could not be canonicalized")
    writer = _open_writer(settings)
    try:
        writer.append(canonical)
    finally:
        writer.close()
    click.echo(f"Accepted: event_id={canonical.event_id}")


@cli.command("ingress-discord")
@click.pass_context
def ingress_discord_cmd(ctx: click.Context) -> None:
    settings = ctx.obj
    writer = _open_writer(settings)
    ingress = DiscordIngress(writer)
    bot = TaskRelayBot(ingress=ingress, settings=settings)
    try:
        bot.run(settings.discord_bot_token.get_secret_value())
    finally:
        writer.close()


@cli.command("ingester")
@click.option("--once", is_flag=True, default=False)
@click.option("--interval", default=1.0, type=float, show_default=True)
@click.pass_obj
def ingester(settings: Settings, once: bool, interval: float) -> None:
    settings.journal_dir.mkdir(parents=True, exist_ok=True)

    def conn_factory() -> sqlite3.Connection:
        return _open_conn(settings)

    warm_conn = conn_factory()
    try:
        _warm_circuit_breaker(warm_conn, conn_factory=conn_factory)
    finally:
        warm_conn.close()

    reader = JournalReader(settings.journal_dir)
    ing = JournalIngester(conn_factory, reader)
    if once:
        click.echo(ing.step())
        return
    ing.run_forever(poll_interval_sec=interval)


@cli.command("router")
@click.option("--once", is_flag=True, default=False)
@click.option("--interval", default=0.5, type=float, show_default=True)
@click.pass_obj
def router(settings: Settings, once: bool, interval: float) -> None:
    conn = _open_conn(settings)
    _warm_circuit_breaker(conn, conn_factory=lambda: _open_conn(settings))
    worker = Router(settings)
    try:
        if once:
            event = queries.fetch_next_unprocessed(conn)
            if event is None:
                click.echo(0)
                return
            worker.run_once(conn, event)
            click.echo(1)
            return
        while True:
            event = queries.fetch_next_unprocessed(conn)
            if event is None:
                time.sleep(interval)
                continue
            worker.run_once(conn, event)
    finally:
        conn.close()


@cli.command("runner")
@click.pass_obj
def runner(settings: Settings) -> None:
    conn = _open_conn(settings)
    try:
        _warm_circuit_breaker(conn, conn_factory=lambda: _open_conn(settings))
    finally:
        conn.close()
    click.echo("Use approve, critical, retry, cancel, unlock, or retry-system.")


@cli.command("projection")
@click.option("--worker-id", default="cli", show_default=True)
@click.option("--once", is_flag=True, default=False)
@click.option("--interval", default=0.5, type=float, show_default=True)
@click.pass_obj
def projection(settings: Settings, worker_id: str, once: bool, interval: float) -> None:
    conn = _open_conn(settings)
    _warm_circuit_breaker(conn, conn_factory=lambda: _open_conn(settings))
    sinks = {
        Stream.TASK_SNAPSHOT: LoggingSink(),
        Stream.TASK_COMMENT: LoggingSink(),
        Stream.TASK_LABEL_SYNC: LoggingSink(),
        Stream.DISCORD_ALERT: LoggingSink(),
    }
    worker = ProjectionWorker(conn, sinks, settings, worker_id=worker_id)
    try:
        if once:
            click.echo(worker.step())
            return
        worker.run_forever(poll_interval_sec=interval)
    finally:
        conn.close()


@cli.command("reconcile")
@click.pass_obj
def reconcile(settings: Settings) -> None:
    try:
        from .reconcile.worker import ReconcileWorker
    except ImportError as exc:
        raise click.ClickException("reconcile worker not yet implemented (Phase 2)") from exc
    conn = _open_conn(settings)
    try:
        worker = ReconcileWorker(conn)
        run_once = getattr(worker, "run_once", None)
        result = run_once() if callable(run_once) else worker.step()
        click.echo(result)
    except (AttributeError, NotImplementedError) as exc:
        raise click.ClickException("reconcile worker not yet implemented (Phase 2)") from exc
    finally:
        conn.close()


@cli.command("retention")
@click.option("--scope", type=click.Choice(["log", "journal", "all"]), default="all", show_default=True)
@click.option("--dry-run", is_flag=True, default=False)
@click.pass_obj
def retention(settings: Settings, scope: str, dry_run: bool) -> None:
    if dry_run:
        click.echo(json.dumps({"dry_run": True, "scope": scope}, ensure_ascii=False, sort_keys=True))
        return
    result: dict[str, Any] = {}
    if scope in {"log", "all"}:
        def conn_factory() -> sqlite3.Connection:
            return _open_conn(settings)

        log_retention = LogRetention(conn_factory, settings.log_dir)
        result["log"] = log_retention.sweep()
    if scope in {"journal", "all"}:
        journal_retention = JournalRetention(settings.journal_dir)
        result["journal_deleted"] = journal_retention.sweep()
    click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))


@cli.command("approve")
@click.option("--task", "task_id", required=True)
@click.option("--actor", required=True)
@click.pass_obj
def approve(settings: Settings, task_id: str, actor: str) -> None:
    request_id = _append_cli_command(settings, event_type="/approve", task_id=task_id, actor=actor)
    click.echo(f"Accepted: request_id={request_id}")


@cli.command("critical")
@click.option("--task", "task_id", required=True)
@click.option("--actor", required=True)
@click.option("--on/--off", "enabled", default=True)
@click.pass_obj
def critical(settings: Settings, task_id: str, actor: str, enabled: bool) -> None:
    event_type = "/critical on" if enabled else "/critical off"
    request_id = _append_cli_command(settings, event_type=event_type, task_id=task_id, actor=actor)
    click.echo(f"Accepted: request_id={request_id}")


@cli.command("retry")
@click.option("--task", "task_id", required=True)
@click.option("--actor", required=True)
@click.option("--replan/--no-replan", default=False)
@click.pass_obj
def retry(settings: Settings, task_id: str, actor: str, replan: bool) -> None:
    event_type = "/retry --replan" if replan else "/retry"
    request_id = _append_cli_command(settings, event_type=event_type, task_id=task_id, actor=actor)
    click.echo(f"Accepted: request_id={request_id}")


@cli.command("cancel")
@click.option("--task", "task_id", required=True)
@click.option("--actor", required=True)
@click.pass_obj
def cancel(settings: Settings, task_id: str, actor: str) -> None:
    request_id = _append_cli_command(settings, event_type="/cancel", task_id=task_id, actor=actor)
    click.echo(f"Accepted: request_id={request_id}")


@cli.command("unlock")
@click.option("--branch", required=True)
@click.option("--actor", required=True)
@click.pass_obj
def unlock(settings: Settings, branch: str, actor: str) -> None:
    request_id = _append_cli_command(
        settings,
        event_type="/unlock",
        task_id=None,
        actor=actor,
        payload={"branch": branch},
    )
    click.echo(f"Accepted: request_id={request_id}")


@cli.command("retry-system")
@click.option("--actor", required=True)
@click.option("--stage", required=True)
@click.pass_obj
def retry_system(settings: Settings, actor: str, stage: str) -> None:
    request_id = _append_cli_command(
        settings,
        event_type="/retry-system",
        task_id=None,
        actor=actor,
        payload={"stage": stage},
    )
    click.echo(f"Accepted: request_id={request_id}")


@cli.command("projection-rebuild")
@click.option("--task", "task_id", required=True)
@click.option("--force/--no-force", default=False)
@click.pass_obj
def projection_rebuild(settings: Settings, task_id: str, force: bool) -> None:
    conn = _open_conn(settings)
    try:
        click.echo(rebuild_for_task(conn, task_id, force=force))
    except NotImplementedError as exc:
        raise click.ClickException("projection rebuild: Phase 2 未実装") from exc
    finally:
        conn.close()


@cli.command("reconcile-report")
@click.option("--last/--all", "last_only", default=True)
@click.pass_obj
def reconcile_report(settings: Settings, last_only: bool) -> None:
    conn = _open_conn(settings)
    try:
        sql = """
            SELECT event_type, severity, payload_json, created_at
            FROM system_events
            ORDER BY id DESC
        """
        if last_only:
            sql += " LIMIT 50"
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    if not rows:
        click.echo("No reconcile events.")
        return
    for row in rows:
        click.echo(f"{row['created_at']} {row['severity']} {row['event_type']} {row['payload_json']}")


@cli.command("status")
@click.pass_obj
def status(settings: Settings) -> None:
    conn = _open_conn(settings)
    try:
        state_rows = conn.execute(
            """
            SELECT state, COUNT(*) AS count
            FROM tasks
            GROUP BY state
            """
        ).fetchall()
        rate_rows = conn.execute(
            """
            SELECT tool_name, remaining, "limit", window_reset_at
            FROM rate_windows
            ORDER BY tool_name ASC
            """
        ).fetchall()
    finally:
        conn.close()
    counts = {row["state"]: int(row["count"]) for row in state_rows}
    state_counts = {state.value: counts.get(state.value, 0) for state in TaskState}
    for state in TaskState:
        click.echo(f"{state.value}={state_counts[state.value]}")
    in_progress = sum(
        state_counts[state.value]
        for state in (
            TaskState.PLANNING,
            TaskState.PLAN_APPROVED,
            TaskState.IMPLEMENTING,
            TaskState.REVIEWING,
        )
    )
    waiting_human = sum(
        state_counts[state.value]
        for state in (
            TaskState.PLAN_PENDING_APPROVAL,
            TaskState.HUMAN_REVIEW_REQUIRED,
            TaskState.NEEDS_FIX,
        )
    )
    global_degraded = state_counts[TaskState.SYSTEM_DEGRADED.value]
    rate_protected = any(
        should_stop_new_tasks(remaining=int(row["remaining"]), limit=int(row["limit"])) for row in rate_rows
    )
    click.echo(f"in_progress={in_progress}")
    click.echo(f"waiting_human={waiting_human}")
    click.echo(f"global_degraded={global_degraded}")
    scope_label = _scope_label(
        in_progress=in_progress,
        waiting_human=waiting_human,
        global_degraded=global_degraded,
        rate_protected=rate_protected,
    )
    click.echo(f"scope_label={scope_label}")
    # WHY: breaker state is in-memory only in Phase 1.
    click.echo("breaker_state=unknown")
    click.echo(f"rate_stop_new_tasks={'on' if rate_protected else 'off'}")
    for row in rate_rows:
        click.echo(
            f"rate[{row['tool_name']}]={row['remaining']}/{row['limit']} reset_at={row['window_reset_at']}"
        )


def main() -> None:
    cli()
