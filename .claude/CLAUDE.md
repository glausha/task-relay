# task-relay プロジェクト固有ルール (親 CLAUDE.md を上書き)

## CodexGate 運用

本ディレクトリ (`task-relay/`) 内での**コード修正 (Edit)** は
Codex に委譲すること。Claude 本体が直接修正することを禁ずる。

- 設計・調査・テスト実行・ビルド確認 (Read / Grep / Glob / Bash) は Claude で行ってよい。
- 実装は粒度を十分に下げた上で切り出して設計し、並行して動かした複数のエージェントで行うこと。
- NITを避けること。
- 新規ファイル作成や既存ファイルの編集は `codex:codex-rescue` エージェント、
  または `/codex:rescue` スラッシュコマンド経由で Codex に依頼する。
- Codex が生成したパッチを Claude 本体が Write / Edit で適用するのは
  以下の条件下で許可する:
  - 実行元が `codex:codex-rescue` エージェント配下であること、または
  - 環境変数 `CODEX_GATE_APPLY=1` がセットされていること。
- Codex 生成物のコミットには `Co-Authored-By: Codex <noreply@openai.com>` を付与する。

Gate の強制は `.claude/hooks/codex-gate.sh` が PreToolUse で実施する。
無効化は `rm -rf .claude/hooks .claude/settings.json` または
`/codex-gate-off` コマンドで行う。

## container-use の扱い

本ディレクトリ内では container-use を**使用しない**。
親 `Glauca/CLAUDE.md` の "ONLY Environments" ルールは task-relay 内では無効。
理由: CodexGate と container-use を重ねると書き込み経路が二重ゲートになり
実用に支障があるため。本格開発フェーズで隔離が必要になったら再検討する。
