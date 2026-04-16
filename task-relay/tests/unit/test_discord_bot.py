from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from task_relay.config import Settings
from task_relay.ingress.discord_bot import TaskRelayBot
from task_relay.ingress.discord_gateway import DiscordIngress
from task_relay.journal.writer import JournalWriter


def test_registers_expected_slash_commands(tmp_path: Path) -> None:
    writer = JournalWriter(tmp_path / "journal")
    bot = TaskRelayBot(
        ingress=DiscordIngress(writer),
        settings=Settings(sqlite_path=tmp_path / "state.sqlite"),
    )

    command_names = sorted(command.name for command in bot.tree.get_commands())

    assert command_names == [
        "approve",
        "cancel",
        "critical",
        "retry",
        "retry-system",
        "status",
        "unlock",
    ]
    writer.close()


async def test_get_requested_by_reads_sqlite(sqlite_conn) -> None:
    now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    sqlite_conn.execute(
        """
        INSERT INTO tasks(
            task_id, source_issue_id, state, state_rev, critical, lease_branch,
            feature_branch, manual_gate_required, worktree_path, last_known_head_commit, resume_target_state,
            requested_by, notification_target, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "task-1",
            None,
            "new",
            0,
            0,
            None,
            None,
            0,
            None,
            None,
            None,
            "discord:123",
            None,
            now.isoformat(),
            now.isoformat(),
        ),
    )
    db_path = Path(sqlite_conn.execute("PRAGMA database_list").fetchone()["file"])
    writer = JournalWriter(db_path.parent / "journal")
    bot = TaskRelayBot(
        ingress=DiscordIngress(writer),
        settings=Settings(sqlite_path=db_path),
    )

    requested_by = await bot._get_requested_by("task-1")

    assert requested_by == "discord:123"
    writer.close()
