---
description: CodexGate を有効化する (settings.json の hooks を復元)
---

task-relay 配下の Write/Edit を Codex 経由のみに制限する CodexGate を有効化します。

手順:
1. `task-relay/.claude/settings.json` を確認し、`hooks.PreToolUse` に
   `codex-gate.sh` が登録されていることを確認する。
2. 無効化時に退避した場合は `settings.json.disabled` → `settings.json` にリネーム。
3. 動作確認として Write を試み、ブロックされることを確認する。
