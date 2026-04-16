# Changelog

## Unreleased (Phase 2 complete)

### Added
- Phase 0-1: package scaffold, DDL, router, ingester, journal, projection, adapters (mock), branch lease, rate, breaker, retention, reconcile, CLI
- Phase 1.5: label 整合, notification_target 分離, Redis lease Lua, breaker durable
- Phase 2: schema v3, adapter transport, ToolRunner subprocess, worktree, Forgejo HTTP, Discord bot, rebuild
- R1-R5 + O1: Runner dispatcher, LLM transport (Anthropic/Claude Code/Codex), git safety, admin unlock/retry-system, projection production sinks, DR CLI

### Test coverage
- 153 unit + integration tests (fakeredis + httpx MockTransport + FakeTransport)

