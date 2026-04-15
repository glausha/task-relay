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

## Phase 別書き込み方針

### Phase 0 (scaffold / DDL / enum / interface skeleton)

- `CODEX_GATE_APPLY=1` を有効化し **Claude 本体が直接 Write** する。
- 対象: `pyproject.toml`, 型/enum 定義, `schema.sql`, 関数 stub, package 雛形。
- 理由: ボイラープレート中心で Codex レビュー価値が薄く、ファイル数が多いため
  hook 経由の逐次 `codex exec` は非効率。
- 実施時は `.claude/settings.local.json` の top-level `env` に
  `{"CODEX_GATE_APPLY":"1"}` を一時的に追加し、Phase 0 完了後に削除する。

### Phase 1 以降 (実ロジック)

- CodexGate を有効化したまま運用する。Claude の Write/Edit は
  `codex-gate.sh` により `codex exec -s danger-full-access --skip-git-repo-check`
  へ自動転送される。
- 例外: Phase 1 内でもスケルトン的な一括変更 (大量の import 整理、
  enum 追加、型追従の mechanical な差分) は `CODEX_GATE_APPLY=1` を
  一時的に使ってよい。判断は粒度で決める。

## サンドボックス経路の扱い

`codex:codex-rescue` サブエージェントは `codex-companion.mjs task --write` を
経由し workspace-write bwrap sandbox を使うため、環境によっては
`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` で失敗する。
失敗した場合は上記 Phase 別方針に切り替え、hook 経由
(`codex exec -s danger-full-access`) または `CODEX_GATE_APPLY=1`
で Claude 直接 Write に fallback する。`codex:codex-rescue` を使う場合でも
失敗時は hook 経路を第一選択とする。
