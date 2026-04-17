from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TASK_RELAY_",
        extra="ignore",
    )

    sqlite_path: Path = Path("var/task-relay/state.sqlite")
    journal_dir: Path = Path("var/task-relay/journal")
    log_dir: Path = Path("var/task-relay/logs")
    redis_url: str = "redis://localhost:6379/0"
    forgejo_base_url: str = "http://localhost:3000"
    forgejo_webhook_host: str = "127.0.0.1"
    forgejo_webhook_port: int = 8787
    forgejo_owner: str = ""
    forgejo_repo: str = ""
    forgejo_token: SecretStr = SecretStr("")
    forgejo_webhook_secret: SecretStr = SecretStr("")
    discord_bot_token: SecretStr = SecretStr("")
    discord_guild_ids: list[int] = Field(default_factory=list)
    admin_user_ids: list[int] = Field(default_factory=list)
    breaker_window_seconds: int = 600
    breaker_fatal_threshold: int = 3
    rate_stop_new_tasks_ratio: float = 0.2
    projection_retry_initial_seconds: int = 60
    projection_retry_cap_seconds: int = 3600
    projection_retry_max_attempts: int = 50
    projection_retry_degrade_hours: int = 24
    projection_stale_claim_seconds: int = 600
    implementing_resume_grace_seconds: int = 120
    implementing_resume_heartbeat_seconds: int = 60
    subprocess_sigterm_grace_seconds: int = 15
    executor_workspace_root: Path = Path("var/task-relay/worktrees")
    planner_timeout_seconds: int = 60
    reviewer_model: str = "gpt-5.4"
    executor_timeout: int = 600
    lease_ttl_seconds: int = 30
    lease_renew_interval_seconds: int = 10
    journal_retain_days: int = 30
    journal_offsite_retain_days: int = 7
    log_retain_full_days: int = 30
    log_retain_digest_days: int = 180
    discord_writer_queue_capacity: int = 1024
    discord_ack_deadline_ms: int = 1500
    litestream_config_path: Path | None = None
