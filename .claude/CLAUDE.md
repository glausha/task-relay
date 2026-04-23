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

## Subagent 運用

`~/.claude/agents/` 配下の global subagent を役割ごとに使い分ける。
description に一致しやすい task は、Claude が自動委譲しやすいように
曖昧な汎用 agent ではなく専門 agent を優先する。
この repo では agent 定義を `.claude/agents/` に重複配置しない。

### 司令塔

- `project-orchestrator` を多段タスクの司令塔とする。
- 役割は task 分解、承認済み routing table からの担当選定、review prompt への規律注入、調停手順の適用、結果統合である。
- `project-orchestrator` 自身に大量実装、レビュー結論の代行、環境修復の抱え込み、進捗%の報告をさせない。
- `project-orchestrator` の routing table は `technical-feasibility-researcher`、
  `ros2-node-developer`, `paper-to-pytorch`, `rigorous-review-devils-advocate`,
  `rigorous-review-persona-advocate`, `rigorous-review-spec-trace-auditor`,
  `rigorous-review-steel-man`, `operations-reviewer`, `conservative-reviewer`,
  `project-infrastructure-manager`,
  `codex:codex-rescue` に固定する。

### 調査と実装

- 不確実性が高い task、新機能、論文再現、RT tuning、infra 変更は
  まず `technical-feasibility-researcher` を通す。
- ROS 2 ノード、launch、QoS、topic 設計は `ros2-node-developer` を使う。
- 論文再現や model の PyTorch 化は `paper-to-pytorch` を使う。
- Docker / Compose / CI / dependency / repo layout は
  `project-infrastructure-manager` に分離する。

### レビュー

- review は reviewer 群を並列に使い、最後に `project-orchestrator` が統合する。
- reviewer は findings を先頭に出し、賛成や要約から始めない。
- `project-orchestrator` は reviewer の指摘を握りつぶさない。
- `project-orchestrator` は 6 reviewer 呼び出し時に 5 pass 規律を prompt へ強制埋め込みする。
  Pass 1-5 は 素点検、30% 撤回、30% 復活、外部一次情報裏取り、Red Team 自己攻撃 とする。
- reviewer が外部事実に依拠する場合、`project-orchestrator` は
  外部一次情報 citation を最低 2 件要求する。検証不能な必須 citation が 1 件でもあれば
  その review を invalid とみなす。
- reviewer 指摘は owner に戻して修正し、必要な reviewer を再実行する。
  actionable な NIT が消えるまでこの loop を回す。
- trivial でない変更は最低 2 視点で review し、仕様、運用、保守、反証のうち
  必要な視点を落とさない。
- `rigorous-review-steel-man` は初手の甘い承認役ではなく、厳しい review 後に
  salvage 可能性を詰める役として使う。
- `rigorous-review-persona-advocate` と `rigorous-review-spec-trace-auditor` は overriding reviewer とし、
  単独 blocker を確定 blocker として扱う。
- `rigorous-review-steel-man`, `rigorous-review-devils-advocate`,
  `operations-reviewer`, `conservative-reviewer` は技術系 reviewer とし、
  同一 blocker への独立収束が 2 件以上で
  confirmed blocker とみなす。単独 blocker は unresolved として再 review または
  human decision に送る。
