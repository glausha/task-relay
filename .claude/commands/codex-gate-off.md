---
description: CodexGate を解除する (hooks を無効化)
---

task-relay プロジェクト初期作成フェーズが終わり、CodexGate が不要になった場合に実行します。

手順:
1. `task-relay/.claude/settings.json` を `settings.json.disabled` にリネーム
   (再有効化したいときに戻せるよう退避)。
2. より恒久的に削除する場合は以下を実行:
   ```
   rm -rf task-relay/.claude/hooks task-relay/.claude/settings.json
   ```
3. `task-relay/.claude/CLAUDE.md` の CodexGate セクションを削除または更新する。
4. 親 `Glauca/CLAUDE.md` の container-use ルールを再度適用するか判断する。
