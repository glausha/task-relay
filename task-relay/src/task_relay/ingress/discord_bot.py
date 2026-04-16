"""Discord bot: detailed-design §3.2, §14."""

from __future__ import annotations

import asyncio
import sqlite3

import discord
from discord import app_commands

from task_relay.config import Settings
from task_relay.db.connection import connect
from task_relay.ingress.discord_gateway import DiscordIngress
from task_relay.rate.windows import should_stop_new_tasks
from task_relay.types import TaskState


class TaskRelayBot(discord.Client):
    def __init__(self, *, ingress: DiscordIngress, settings: Settings):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._ingress = ingress
        self._settings = settings
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(name="approve", description="Approve a task plan")
        @app_commands.describe(task_id="Task ID")
        async def approve(interaction: discord.Interaction, task_id: str) -> None:
            await self._handle_journal_command(interaction, command="/approve", task_id=task_id)

        @self.tree.command(name="critical", description="Mark a task as critical")
        @app_commands.describe(task_id="Task ID")
        async def critical(interaction: discord.Interaction, task_id: str) -> None:
            await self._handle_journal_command(interaction, command="/critical on", task_id=task_id)

        @self.tree.command(name="retry", description="Retry a task")
        @app_commands.describe(task_id="Task ID")
        async def retry(interaction: discord.Interaction, task_id: str) -> None:
            await self._handle_journal_command(interaction, command="/retry", task_id=task_id)

        @self.tree.command(name="cancel", description="Cancel a task")
        @app_commands.describe(task_id="Task ID")
        async def cancel(interaction: discord.Interaction, task_id: str) -> None:
            await self._handle_journal_command(interaction, command="/cancel", task_id=task_id)

        @self.tree.command(name="unlock", description="Unlock a branch")
        @app_commands.describe(branch="Lease branch")
        async def unlock(interaction: discord.Interaction, branch: str) -> None:
            await self._handle_journal_command(
                interaction,
                command="/unlock",
                task_id=None,
                extra_payload={"branch": branch},
            )

        @self.tree.command(name="retry-system", description="Retry a degraded system stage")
        @app_commands.describe(stage="Stage name")
        async def retry_system(interaction: discord.Interaction, stage: str) -> None:
            await self._handle_journal_command(
                interaction,
                command="/retry-system",
                task_id=None,
                extra_payload={"stage": stage},
            )

        @self.tree.command(name="status", description="Show task relay status")
        async def status(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(await self._build_status_message(), ephemeral=True)

    async def _handle_journal_command(
        self,
        interaction: discord.Interaction,
        *,
        command: str,
        task_id: str | None,
        extra_payload: dict[str, str] | None = None,
    ) -> None:
        message, _ = await self._ingress.handle_slash_command(
            command=command,
            user_id=interaction.user.id,
            task_id=task_id,
            extra_payload=extra_payload,
            admin_user_ids=self._settings.admin_user_ids,
            get_requested_by=self._get_requested_by,
        )
        await interaction.response.send_message(message, ephemeral=True)

    async def _get_requested_by(self, task_id: str) -> str | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._load_requested_by, task_id)

    def _load_requested_by(self, task_id: str) -> str | None:
        conn = self._connect_db()
        try:
            row = conn.execute("SELECT requested_by FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()
        return None if row is None else str(row["requested_by"])

    async def _build_status_message(self) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._load_status_message)

    def _load_status_message(self) -> str:
        conn = self._connect_db()
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
        except sqlite3.OperationalError:
            state_rows = []
            rate_rows = []
        finally:
            conn.close()
        counts = {row["state"]: int(row["count"]) for row in state_rows}
        state_counts = {state.value: counts.get(state.value, 0) for state in TaskState}
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
        lines = [f"{state.value}={state_counts[state.value]}" for state in TaskState]
        lines.append(f"in_progress={in_progress}")
        lines.append(f"waiting_human={waiting_human}")
        lines.append(f"global_degraded={global_degraded}")
        lines.append(
            f"scope_label={self._scope_label(in_progress, waiting_human, global_degraded, rate_protected)}"
        )
        lines.append("breaker_state=unknown")
        lines.append(f"rate_stop_new_tasks={'on' if rate_protected else 'off'}")
        for row in rate_rows:
            lines.append(
                f"rate[{row['tool_name']}]={row['remaining']}/{row['limit']} reset_at={row['window_reset_at']}"
            )
        return "\n".join(lines)

    def _connect_db(self) -> sqlite3.Connection:
        self._settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return connect(self._settings.sqlite_path)

    @staticmethod
    def _scope_label(
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

    async def setup_hook(self) -> None:
        for guild_id in self._settings.discord_guild_ids:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def close(self) -> None:
        await self._ingress.stop()
        await super().close()
