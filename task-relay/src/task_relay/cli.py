from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable
from pathlib import Path

import click
import discord
import redis
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
from .projection.discord_sink import DiscordSink
from .projection.forgejo_sink import ForgejoSink
from .projection.rebuild import rebuild_for_task
from .projection.worker import ProjectionWorker
from .retention.journal_retention import JournalRetention
from .retention.log_retention import LogRetention
from .router.router import Router
from .status import load_status_snapshot
from .types import Stream

LOGGER = logging.getLogger(__name__)


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


def _run_router_post_apply(
    event,
    result,
    *,
    unlock_handler,
    retry_system_handler,
) -> None:
    if result.skipped:
        return
    if event.event_type == "/unlock":
        branch = event.payload.get("branch")
        if branch is not None:
            unlock_handler.handle_unlock(str(branch))
        return
    if event.event_type == "/retry-system":
        stage = event.payload.get("stage")
        retry_system_handler.handle_retry_system(None if stage is None else str(stage))


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


@dataclass(frozen=True)
class _ProjectionDiscordRuntime:
    client: discord.Client
    loop: asyncio.AbstractEventLoop
    thread: threading.Thread

    def close(self) -> None:
        if not self.thread.is_alive():
            return
        if not self.loop.is_closed() and not self.client.is_closed():
            asyncio.run_coroutine_threadsafe(self.client.close(), self.loop).result(timeout=10)
        self.thread.join(timeout=10)


def _start_projection_discord_runtime(token: str) -> _ProjectionDiscordRuntime:
    started = threading.Event()
    state: dict[str, Any] = {}

    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = discord.Client(intents=discord.Intents.default())
        state["loop"] = loop
        state["client"] = client
        loop.call_soon(started.set)
        try:
            loop.run_until_complete(client.start(token))
        except Exception as exc:
            state["error"] = exc
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    thread = threading.Thread(target=runner, name="task-relay-projection-discord", daemon=True)
    thread.start()
    if not started.wait(timeout=10):
        raise click.ClickException("discord projection client did not start")
    error = state.get("error")
    if error is not None:
        raise click.ClickException(f"discord projection client failed to start: {error}")
    client = state.get("client")
    loop = state.get("loop")
    if not isinstance(client, discord.Client) or not isinstance(loop, asyncio.AbstractEventLoop):
        raise click.ClickException("discord projection client initialization failed")
    try:
        # WHY: projection DM delivery must wait for a live gateway session before sending.
        asyncio.run_coroutine_threadsafe(client.wait_until_ready(), loop).result(timeout=10)
    except Exception as exc:
        if not loop.is_closed() and not client.is_closed():
            asyncio.run_coroutine_threadsafe(client.close(), loop).result(timeout=10)
        thread.join(timeout=10)
        raise click.ClickException(f"discord projection client did not become ready: {exc}") from exc
    return _ProjectionDiscordRuntime(client=client, loop=loop, thread=thread)


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


@cli.command("db-check")
@click.pass_obj
def db_check_cmd(settings: Settings) -> None:
    """SQLite integrity check + schema apply for startup."""
    conn = _open_conn(settings)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            detail = "no result" if result is None else str(result[0])
            raise click.ClickException(f"Integrity check failed: {detail}")
        apply_schema(conn)
    finally:
        conn.close()
    click.echo("db-check: ok")


@cli.command("journal-replay")
@click.pass_obj
def journal_replay_cmd(settings: Settings) -> None:
    """Replay journal into inbox from persisted ingester state."""
    reader = JournalReader(settings.journal_dir)
    ingester = JournalIngester(lambda: _open_conn(settings), reader)
    count = 0
    while True:
        imported = ingester.step()
        if imported == 0:
            break
        count += imported
    click.echo(f"journal-replay: {count} events ingested")


@cli.command("health-check")
@click.pass_obj
def health_check_cmd(settings: Settings) -> None:
    """Check SQLite, Redis, and journal directory readiness."""
    errors: list[str] = []
    try:
        conn = _open_conn(settings)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:
        errors.append(f"sqlite: {exc}")

    redis_client: Any | None = None
    try:
        redis_client = redis.from_url(settings.redis_url)
        redis_client.ping()
    except Exception as exc:
        errors.append(f"redis: {exc}")
    finally:
        close = getattr(redis_client, "close", None)
        if callable(close):
            close()

    if not settings.journal_dir.is_dir():
        errors.append(f"journal_dir not found: {settings.journal_dir}")

    if errors:
        for error in errors:
            click.echo(f"FAIL: {error}", err=True)
        raise SystemExit(1)
    click.echo("health-check: ok")


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
    from .branch_lease.redis_lease import RedisLease
    from .runner.retry_system_handler import RetrySystemHandler
    from .runner.unlock_handler import UnlockHandler

    def conn_factory() -> sqlite3.Connection:
        return _open_conn(settings)

    conn = conn_factory()
    breaker = _warm_circuit_breaker(conn, conn_factory=conn_factory)
    worker = Router(settings)
    writer = _open_writer(settings)
    redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    unlock_handler = UnlockHandler(conn_factory, RedisLease(redis_client, settings), writer)
    retry_system_handler = RetrySystemHandler(
        breaker,
        writer,
        conn_factory=conn_factory,
        redis_client=redis_client,
    )
    try:
        if once:
            event = queries.fetch_next_unprocessed(conn)
            if event is None:
                click.echo(0)
                return
            result = worker.run_once(conn, event)
            _run_router_post_apply(
                event,
                result,
                unlock_handler=unlock_handler,
                retry_system_handler=retry_system_handler,
            )
            click.echo(1)
            return
        while True:
            event = queries.fetch_next_unprocessed(conn)
            if event is None:
                time.sleep(interval)
                continue
            result = worker.run_once(conn, event)
            _run_router_post_apply(
                event,
                result,
                unlock_handler=unlock_handler,
                retry_system_handler=retry_system_handler,
            )
    finally:
        writer.close()
        close = getattr(redis_client, "close", None)
        if callable(close):
            close()
        conn.close()


@cli.command("runner")
@click.option("--once", is_flag=True, default=False)
@click.option("--task-id", default=None)
@click.pass_context
def runner_cmd(ctx: click.Context, once: bool, task_id: str | None) -> None:
    settings = ctx.obj
    from .runner.adapters.planner import PlannerAdapter
    from .runner.adapters.reviewer import ReviewerAdapter
    from .runner.dispatcher import TaskDispatcher
    from .runner.transports.anthropic_transport import AnthropicTransport
    from .runner.transports.codex_transport import CodexTransport

    def conn_factory() -> sqlite3.Connection:
        return _open_conn(settings)

    redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    writer = _open_writer(settings)
    planner = PlannerAdapter(
        AnthropicTransport(
            model=settings.planner_model,
            max_tokens=settings.planner_max_tokens,
        )
    )
    reviewer = ReviewerAdapter(CodexTransport(model=settings.reviewer_model))
    dispatcher = TaskDispatcher(
        conn_factory=conn_factory,
        journal_writer=writer,
        settings=settings,
        redis_client=redis_client,
        planner=planner,
        reviewer=reviewer,
    )
    try:
        if task_id:
            dispatcher.run_task(task_id)
            return
        if once:
            click.echo(str(dispatcher.step()))
            return
        dispatcher.run_forever()
    finally:
        writer.close()


@cli.command("projection")
@click.option("--worker-id", default="cli", show_default=True)
@click.option("--once", is_flag=True, default=False)
@click.option("--interval", default=0.5, type=float, show_default=True)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--with-discord", is_flag=True, default=False)
@click.pass_obj
def projection(
    settings: Settings,
    worker_id: str,
    once: bool,
    interval: float,
    dry_run: bool,
    with_discord: bool,
) -> None:
    conn = _open_conn(settings)
    _warm_circuit_breaker(conn, conn_factory=lambda: _open_conn(settings))
    discord_runtime: _ProjectionDiscordRuntime | None = None
    if dry_run:
        sinks = {stream: LoggingSink() for stream in Stream}
    else:
        forgejo_sink = ForgejoSink(
            base_url=settings.forgejo_base_url,
            token=settings.forgejo_token.get_secret_value(),
            owner=settings.forgejo_owner,
            repo=settings.forgejo_repo,
            conn=conn,
        )
        discord_sink = DiscordSink(admin_user_ids=settings.admin_user_ids)
        if with_discord:
            token = settings.discord_bot_token.get_secret_value()
            if not token:
                LOGGER.warning(
                    "projection requested --with-discord but TASK_RELAY_DISCORD_BOT_TOKEN is empty; continuing without discord client"
                )
            else:
                discord_runtime = _start_projection_discord_runtime(token)
                discord_sink = DiscordSink(
                    client=discord_runtime.client,
                    loop=discord_runtime.loop,
                    admin_user_ids=settings.admin_user_ids,
                )
        sinks = {
            Stream.TASK_SNAPSHOT: forgejo_sink,
            Stream.TASK_COMMENT: forgejo_sink,
            Stream.TASK_LABEL_SYNC: forgejo_sink,
            Stream.DISCORD_ALERT: discord_sink,
        }
    worker = ProjectionWorker(conn, sinks, settings, worker_id=worker_id)
    try:
        if once:
            click.echo(worker.step())
            return
        worker.run_forever(poll_interval_sec=interval)
    finally:
        if discord_runtime is not None:
            discord_runtime.close()
        conn.close()


@cli.command("reconcile")
@click.pass_obj
def reconcile(settings: Settings) -> None:
    from .reconcile.worker import ReconcileWorker

    writer = _open_writer(settings)
    try:
        worker = ReconcileWorker(
            conn_factory=lambda: _open_conn(settings),
            journal_writer=writer,
            implementing_grace_seconds=settings.implementing_resume_grace_seconds,
            heartbeat_seconds=settings.implementing_resume_heartbeat_seconds,
        )
        result = worker.run_once()
        click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        writer.close()


@cli.command("retention")
@click.option("--scope", type=click.Choice(["log", "journal", "all"]), default="all", show_default=True)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def retention(settings: Settings, scope: str, dry_run: bool, as_json: bool) -> None:
    if dry_run:
        payload = {"dry_run": True, "scope": scope}
        if as_json:
            click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            click.echo(f"dry_run scope={scope}")
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
    log_result = result.get("log")
    if isinstance(log_result, dict):
        orphan_files = int(log_result.get("orphan_files", 0))
        stale_metadata = int(log_result.get("stale_metadata", 0))
        if orphan_files > 0 or stale_metadata > 0:
            click.echo(
                f"retention warnings: orphan_files={orphan_files} stale_metadata={stale_metadata}",
                err=True,
            )
    if as_json:
        click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        click.echo(f"retention scope={scope} result={json.dumps(result, ensure_ascii=False, sort_keys=True)}")


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
        count = rebuild_for_task(conn, task_id, force=force)
        click.echo(f"Rebuilt {count} outbox rows for {task_id}")
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
        snapshot = load_status_snapshot(conn, settings)
    finally:
        conn.close()
    for line in snapshot.render_lines():
        click.echo(line)


def main() -> None:
    cli()
