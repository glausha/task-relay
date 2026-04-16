# task-relay

Webhook / Gateway 駆動の AI agent タスクランナー。基本設計・詳細設計はリポジトリ親ディレクトリの `basic-design-v1.0.md`, `detailed-design-v1.0.md`, `docs/reference/` を参照してください。

```
uv sync
uv run task-relay --help
```

## Quickstart

```
uv sync
cp deploy/.env.example /etc/task-relay/task-relay.env  # 編集
uv run task-relay db-check
uv run task-relay runner --once
```
