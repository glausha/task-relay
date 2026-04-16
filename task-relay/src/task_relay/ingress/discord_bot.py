"""Discord bot: detailed-design §3.2, §14."""

from __future__ import annotations

import asyncio
import sqlite3

import discord
from discord import app_commands

from task_relay.config import Settings
from task_relay.db.connection import connect
from task_relay.ingress.discord_gateway import DiscordIngress
from task_relay.status import empty_status_snapshot, load_status_snapshot


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
            snapshot = load_status_snapshot(conn, self._settings)
        except sqlite3.OperationalError:
            snapshot = empty_status_snapshot()
        finally:
            conn.close()
        return "\n".join(snapshot.render_lines())

    def _connect_db(self) -> sqlite3.Connection:
        self._settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return connect(self._settings.sqlite_path)

    async def setup_hook(self) -> None:
        for guild_id in self._settings.discord_guild_ids:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def close(self) -> None:
        await self._ingress.stop()
        await super().close()
