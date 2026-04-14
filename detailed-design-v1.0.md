# task-relay 詳細設計 v1.0

本書は `basic-design-v1.0.md` (基本設計) と対になる詳細設計文書である。抽象的な原則・契約・責務境界は基本設計を参照し、本書は具体的な列定義、閾値、アルゴリズム、実装手順、IPC 経路、運用規則を扱う。

基本設計との関係:

- 基本設計: アーキテクチャ、不変条件、責務境界、契約、状態モデル骨格、非機能要件
- 詳細設計: SQLite schema、Router transaction 手順、状態機械の Trigger/Guard 表、retry 閾値、Circuit Breaker 閾値、Projection 冪等性アルゴリズム、Cold Start 手順、redact allowlist

参照ビューとの関係:

- `docs/reference/schema.md`, `docs/reference/state-machine.md`, `docs/reference/reconcile.md`, `docs/reference/disaster-recovery.md` は、本書の該当章を人間が引きやすい形へ切り出した参照ビューである。
- 実装仕様の source of truth は本書であり、参照ビューと矛盾した場合は本書が勝つ。
- 参照ビューは本書の置き換えではなく、要点整理と運用参照のためにだけ存在する。

## 0. ペルソナ・トレーサビリティ

本書の各詳細規則は [personas.md](personas.md) の要求を実装可能な形へ落としている。
設計判断を含む `##` / `###` 節には `主要受益ペルソナ` を必須で注記し、レビュー時に「この閾値や手順は誰のためにあるか」を辿れるようにする。
例外は `関連ドキュメント`, 純粋な補助列挙, file path や enum のみを示す参照補助節だけとする。

対応原則:

- P1: モバイル中心でも安全に投入・把握・最小介入できること
- P2: 深夜でも局所障害と全体障害を誤読せず、止める・待つ・再開するを選べること
- P3: 後から状態遷移、再送、介入理由を説明できること
- P4: upgrade、restore、retention を runbook どおり反復できること

---

## 1. 書き込み順序

主要受益ペルソナ: P3, P4

### 1.1 Ingress

主要受益ペルソナ: P1, P3

```
署名検証 -> ingress_journal append+fsync -> ACK/defer -> SQLite inbox ingest
```

### 1.2 業務状態遷移

主要受益ペルソナ: P3

```
SQLite transaction (outbox INSERT + inbox.processed_at 更新 + state 更新) -> commit
```

### 1.3 外部反映

主要受益ペルソナ: P3

```
projection_outbox -> Forgejo / Discord
```

### 1.4 SQLite 破損・満杯時の具体手順

主要受益ペルソナ: P2, P4

- 新規受理を停止し Discord に `system_degraded` を通知する。
- Router / worker は drain せず停止する。
- 実行中 ToolRunner 親プロセスは subprocess に SIGTERM を送る。
- 15 秒以内に終了しなければ SIGKILL する。
- 停止した call は `tool_calls.failure_code = system_degraded` として記録する。
- 復旧後に journal replay から再開する。

---

## 2. データモデル詳細

主要受益ペルソナ: P3, P4

### 2.1 主テーブル定義

主要受益ペルソナ: P3, P4

#### `tasks`

- `task_id`
- `source_issue_id`
- `state`
- `state_rev`
- `critical`
- `current_branch`
- `manual_gate_required`
- `last_known_head_commit`
- `resume_target_state`
- `requested_by`
- `created_at`
- `updated_at`

規則:

- `manual_gate_required` は自動で false にならない。解除は `/approve` または `runner-cli approve` のみ。
- `resume_target_state` は `* -> system_degraded` 遷移時に現在 state を退避し、復帰時に clear する。

#### `plans`

- `task_id`, `plan_rev`, `planner_version`, `plan_json`
- `validator_score`, `validator_errors`
- `approved_by`, `approved_at`, `approved_kind` (`auto`, `manual`)
- `created_at`

#### `event_inbox`

- `event_id`, `source`, `delivery_id`, `event_type`, `payload_json`
- `journal_offset`
- `received_at`, `processed_at`
- unique(`source`, `delivery_id`)

#### `projection_outbox`

- `outbox_id`
- `task_id`
- `stream` (`task_snapshot`, `task_comment`, `task_label_sync`, `discord_alert`)
- `target`
- `origin_event_id`
- `payload_json`
- `state_rev`
- `idempotency_key`
- `claimed_by`, `claimed_at`
- `attempt_count`, `next_attempt_at`, `sent_at`
- unique(`stream`, `target`, `idempotency_key`)

#### `projection_cursors`

- `task_id`, `stream`, `target`
- `last_sent_state_rev`
- `last_sent_outbox_id`
- `updated_at`
- primary key(`task_id`, `stream`, `target`)

#### `branch_waiters`

- `branch`, `task_id`, `queue_order`, `status`
- `queue_order` は enqueue 時に SQLite 単調増加 INTEGER を採番。時刻は使わない。
- `status` enum: `queued`, `leased`, `paused_system_degraded`, `removed`。

#### `branch_tokens`

- `branch`, `last_token`

#### `journal_ingester_state`

- `singleton_id` (常に 1 行)
- `last_file`, `last_offset`
- `updated_at`
- 更新は `event_inbox` 反映 transaction と同時に commit する。

#### `tool_calls`

- `call_id`, `task_id`, `stage`, `tool_name`
- `started_at`, `ended_at`, `duration_ms`
- `success`, `exit_code`, `failure_code`
- `log_path`, `log_sha256`, `log_bytes`
- `tokens_in`, `tokens_out`

#### `rate_windows`

- `tool_name`, `window_started_at`, `window_reset_at`, `remaining`, `limit`, `updated_at`

#### `system_events`

- `task_id`, `event_type`, `severity`, `payload_json`, `created_at`
- 代表的な `event_type` には `mirror_readonly_violation_detected`, `retention_orphan_detected` を含む。

### 2.2 補助ファイル

主要受益ペルソナ: P3, P4

- `var/task-relay/journal/YYYYMMDD.ndjson.zst`
- `var/task-relay/logs/<task_id>/<stage>/<started_at>_<call_id>.jsonl.zst`

- `raw_events` を SQLite に保持しない。SQLite には path, hash, size だけを保存する。
- journal は日単位 rotate + zstd 圧縮。
- tool log は task / stage / call 単位で 1 ファイル。

---

## 3. Ingress 詳細

主要受益ペルソナ: P1, P2

### 3.1 Forgejo Webhook

主要受益ペルソナ: P1, P3

受理順序:

```
Forgejo -> HMAC verify -> journal append+fsync -> 2xx -> inbox ingest
```

ルール:

- `reviewing -> done` 判定に `pull_request` Webhook は使わない。
- issue body frontmatter の人間編集は真実にしない。差分検知時は `system comment` で「mirror は読み取り専用」と通知する。
- frontmatter diff 検知は `task_snapshot` projection worker が issue body 更新時に観測目的で行う。Router の状態判定には使わない。
- frontmatter diff を検知した projection worker は、`system_events` に `event_type=mirror_readonly_violation_detected`, `severity=warning` を append する。`payload_json` には少なくとも `issue_id`, `changed_fields`, `observed_remote_updated_at` を含める。
- allowlist label の追加イベントと削除イベントはどちらも受理する。label desired set は SQLite 真実から再計算する。

### 3.2 Discord Gateway

主要受益ペルソナ: P1, P2

実装パラメタ:

- writer queue 上限: gateway process 全体で 1024 件。
- handler は append 成功 future を最大 1500ms 待つ。
- queue 満杯 / append 失敗 / 1500ms 超過で失敗応答を返し受理しない。
- SQLite `event_inbox` への ingest は別 worker が非同期に実施する。

失敗 UX:

- `request_id` は ingress handler が append 前に採番し、journal record に埋め込む。
- journal append 前には `task_id` を確定しない。失敗応答には `request_id` のみを含める。
- 既知の `task_id` を client が入力していても、ingest 前は存在性確認できないため応答へ含めない。
- ephemeral message テンプレート:
  - queue 満杯: `Task relay is busy and could not accept this request. request_id=<request_id>`
  - fsync 失敗: `Task relay could not durably record this request. request_id=<request_id>`
  - 1500ms 超過: `Task relay did not confirm durable acceptance in time. request_id=<request_id>`
- Discord client 側自動リトライは行わない。再送は人間が明示的に再実行する。

`/status` query:

- `/status` は read-only query であり、task truth を変更しないため journal append を行わない。
- source of truth は SQLite truth と breaker state 観測値である。
- 最低限の出力 schema:
  - `scope_label`: `進行中` / `人間待ち` / `局所保留` / `局所障害` / `局所停止` / `全体障害` / `全体保護中` / `完了`
  - `summary_counts`: `in_progress`, `waiting_human`, `local_attention`, `global_degraded`, `global_protected`
  - `top_tasks[]`: `task_id`, `state`, `scope_label`, `next_action`, `url`
  - `system_message`: 全体影響がある場合の短い説明
- `/status` は少なくとも `局所障害`, `全体障害`, `全体保護中` を区別して表示しなければならない。

### 3.3 runner-cli

主要受益ペルソナ: P1, P2, P4

- すべての管理操作は `cli` source event として journal → inbox を通す。直接 DB を書き換えない。

---

## 4. Inbox / Router 処理詳細

主要受益ペルソナ: P2, P3

### 4.1 Inbox 処理 transaction

主要受益ペルソナ: P3

手順:

1. `event_inbox` から未処理 event を 1 件取得する。
2. 状態遷移 / plan 更新 / outbox 生成を行う。
3. `event_inbox.processed_at` を更新する。
4. commit する。

意味論:

- `projection_outbox` INSERT と `event_inbox.processed_at` 更新は同一 transaction に入る。
- commit 前クラッシュでは両方ともロールバックされる。commit 後クラッシュでは両方永続化される。
- commit 境界の中間状態は存在しない。commit 前に止まった event は未処理のまま残り再実行される。
- Router は同一 `event_id` に対して決定的な `projection_outbox.payload_json` を生成しなければならない。
- Router は乱数や wall-clock 現在時刻を使ってはならない。
- Router が transaction 内で書く時刻は `event_inbox.received_at` を基準時刻として使う。
- `tasks.updated_at`, `plans.approved_at`, `system_events.created_at` は基準時刻そのものを記録する。
- 初回 `projection_outbox.next_attempt_at` は `event_inbox.received_at + initial_retry_delay` で計算する。

### 4.2 冪等性

主要受益ペルソナ: P3

- 受理段階: unique(`source`, `delivery_id`)。
- Router 段階: `state_rev` と現在 state を見て無効遷移を no-op にする。
- Projection 段階:
  - すべての outbox 行に `idempotency_key` を付与する。
  - `task_snapshot` の supersede 判定は `projection_cursors.last_sent_state_rev` だけで行う。
  - `task_comment` / `discord_alert` は FIFO 厳守で drop しない。
  - Forgejo issue comment には hidden marker `<!-- task-relay:idempotency_key=<key> -->` を埋め込む。
  - Discord alert には bot 管理用 footer `relay_idempotency_key=<key>` を含める。
  - `sent_at` 未更新で worker が再起動した場合、comment / alert 系のみ remote を検索して同 key があれば再投稿せず `sent_at` を補完する。

`idempotency_key` 生成:

- `sha256` による決定的生成とする。
- key material は UTF-8 文字列を `\x1f` 区切りで連結し、集合はソート済みで直列化する。
- stream ごとの生成規則:
  - `task_snapshot`: `sha256(task_id | task_snapshot | target | state_rev | canonical_payload_json_sha256)`
  - `task_comment`: `sha256(task_id | task_comment | target | origin_event_id | comment_kind)`
  - `task_label_sync`: `sha256(task_id | task_label_sync | target | state_rev | sorted(desired_label_set))`
  - `discord_alert`: `sha256(task_id | discord_alert | target | alert_kind | state_rev)`
- rebuild は同じ key material を再計算し同じ `idempotency_key` を再利用する。
- remote lookup は「送信済みか不明な comment / alert の重複防止」にだけ使う。
- snapshot の状態順序判定に Forgejo mirror を使ってはならない。

### 4.3 状態機械

主要受益ペルソナ: P2, P3

#### 4.3.1 正常系遷移

| From | Trigger | Guard | To |
|---|---|---|---|
| `new` | task 作成を受理 | なし | `planning` |
| `planning` | planner が有効な plan を返す | auto approve 条件を満たす | `plan_approved` |
| `planning` | planner が有効な plan を返す | auto approve 条件を満たさない | `plan_pending_approval` |
| `plan_pending_approval` | `/approve` | plan_rev が現行一致 | `plan_approved` |
| `plan_approved` | dispatch 実行 | branch lease 取得成功 | `implementing` |
| `plan_approved` | dispatch 実行 | branch lease 未取得 | `plan_approved` |
| `implementing` | `internal.executor_finished` | exit_code=0 かつ changed_files が許容範囲内 | `reviewing` |
| `reviewing` | reviewer が `decision=pass` を返す | `unchecked=0` かつ `manual_gate_required = false` | `done` |
| `reviewing` | reviewer が `decision=pass` を返す | `unchecked=0` かつ `manual_gate_required = true` | `human_review_required` |

#### 4.3.2 例外系遷移

| From | Trigger | Guard | To |
|---|---|---|---|
| `planning` | validator failure が規定回数超過 | なし | `human_review_required` |
| `planning` | `internal.infra_fatal` / breaker open | なし | `system_degraded` |
| `plan_pending_approval` | `/critical on` | `critical=false` | `plan_pending_approval` |
| `plan_pending_approval` | `/retry --replan` | なし | `planning` |
| `plan_pending_approval` | `/cancel` | なし | `cancelled` |
| `plan_approved` | `/critical on` | なし | `plan_pending_approval` |
| `implementing` | `internal.executor_finished` | 範囲外変更あり | `needs_fix` |
| `implementing` | `internal.executor_finished` | infra failure ではない一般エラー | `needs_fix` |
| `implementing` | `internal.reconcile_resume` | dirty worktree かつ再開条件を満たす | `implementing_resume_pending` |
| `implementing` | `internal.lease_lost` | なし | `human_review_required` |
| `implementing` | `internal.infra_fatal` / breaker open | なし | `system_degraded` |
| `implementing_resume_pending` | 同一 task が grace 内に再開要求 | HEAD / plan_rev / allowed_files が一致 | `implementing` |
| `implementing_resume_pending` | 120 秒経過 | なし | `human_review_required` |
| `needs_fix` | `/retry` | 同一 `plan_rev` を継続使用 | `implementing` |
| `needs_fix` | `/retry --replan` または Router が再計画必要と判断 | なし | `planning` |
| `reviewing` | reviewer が `decision=fail` を返す | なし | `needs_fix` |
| `reviewing` | reviewer が `decision=human_review_required` を返す | なし | `human_review_required` |
| `human_review_required` | `/approve` | review 済みで `manual_gate_required = true` | `done` |
| `human_review_required` | `/retry` | 実装再試行を選ぶ | `implementing` |
| `human_review_required` | `/retry --replan` | 再計画を選ぶ | `planning` |
| `system_degraded` | `internal.system_recovered` | root cause 解消済みかつ `resume_target_state` 非 null | `resume_target_state` に戻す |
| `*` | `/cancel` | `done` 以外 | `cancelled` |

#### 4.3.3 遷移補足

- self-loop や flag 変更のみの遷移でも task truth が変わるため `state_rev` は 1 増やす。
- `* -> system_degraded` 遷移時、Router は現在の `tasks.state` を `tasks.resume_target_state` に退避してから `state=system_degraded` に更新する。
- `system_degraded` から復帰した transaction で `resume_target_state` は null に clear する。
- `system_degraded` からの復帰では、Router は `resume_target_state` の進入 guard を再評価する。
- 復帰先が `implementing` の場合、lease / subprocess 継続前提は失われているため `plan_approved` へフォールバックし wait queue に再 enqueue する。
- 復帰先が `reviewing` の場合、以前の reviewer 実行結果は再利用せず reviewer を再 dispatch する。
- 復帰先が dispatchable でない、または必要な health check を満たさない場合は `human_review_required` に送る。
- `done` と `cancelled` は projection 永久失敗だけを理由に `system_degraded` へ遷移させてはならない。

### 4.4 `/critical` 規則

主要受益ペルソナ: P1, P2

| 現状態 | `/critical on` の結果 |
|---|---|
| `new`, `planning`, `plan_pending_approval` | `critical=true` に設定。以後 auto approve 無効 |
| `plan_approved` | `critical=true` に設定し `plan_pending_approval` へ戻す |
| `implementing` | `critical=true`, `manual_gate_required=true`。現在の実装は継続可だが `reviewing -> done` 自動遷移を禁止 |
| `reviewing` | `critical=true`, `manual_gate_required=true` |
| `done` | 将来 task に影響しない。監査のみ |

`/critical off` 規則:

- `critical` フラグは自動で false にならない。
- `/critical off` は task の `requested_by` または `admin_user_ids` の明示操作でのみ成立する。
- `/critical off` は `manual_gate_required` を暗黙には解除しない。

### 4.5 Internal Event Types

主要受益ペルソナ: P2, P3

主要な internal event_type:

- `internal.executor_finished`
- `internal.lease_lost`
- `internal.infra_fatal`
- `internal.reconcile_resume`
- `internal.system_recovered`
- `internal.unlock_requested`

これらはすべて `ingress_journal -> event_inbox -> Router` を通して処理する。

---

## 5. Branch Lease 実装

主要受益ペルソナ: P1, P2

### 5.1 取得

主要受益ペルソナ: P1, P2, P3

- 待機列の真実は SQLite `branch_waiters`。
- `queue_order` 最小の task のみ lease 取得を試みる。
- `fencing_token` は SQLite `branch_tokens` を transaction で `last_token += 1` して採番する。
- Redis key:
  - `lease:branch:<branch>` = `{task_id, fencing_token, expires_at}`
  - TTL 30 秒

### 5.2 heartbeat / renew

主要受益ペルソナ: P1, P2

- ToolRunner 親プロセスが subprocess を起動する。
- 親は別 asyncio task で 10 秒ごとに compare-and-renew を実行する。
- subprocess は Redis に対して lease key の read-only assert だけを行ってよい。
- subprocess は renew / release / token 採番を行ってはならない。
- subprocess 終了時に renew task も停止する。

### 5.3 実行中の強制検査

主要受益ペルソナ: P1, P2, P3

- 各 Git mutate 前に `assert_lease_readonly(branch, task_id, fencing_token)` を呼ぶ。
- token 不一致または lease 欠落時は mutate を開始してはならない。
- 各 Git mutate 成功直後に、親プロセスは SQLite transaction で `tasks.last_known_head_commit = <new HEAD sha>` を更新する。この更新は operational metadata 更新であり、`tasks.state` と `state_rev` は変更しない。
- 親プロセスの renew が 2 回連続失敗したら親は subprocess を即時停止する。
- 停止後、親プロセスは `internal.lease_lost` event を `ingress_journal` に append する。
- journal ingester がその event を `event_inbox` に取り込み、Router が `human_review_required` 遷移を実行する。
- 親プロセスは Redis channel `task_state_changed:<task_id>` を subscribe し、fallback として 1 秒間隔で `tasks.state` を polling する。
- 親プロセスが `cancelled` / `system_degraded` / `human_review_required` のいずれかを観測したら subprocess に SIGTERM を送り、15 秒以内に終了しなければ SIGKILL する。

### 5.4 解放

主要受益ペルソナ: P2, P4

- 正常終了時に compare-and-delete。
- `cancelled`, `done`, `human_review_required` は wait queue から除去する。
- `system_degraded` は短期復帰前提のため直ちには wait queue から除去しない。
- `system_degraded` が 24 時間継続した task は reconcile が wait queue から除去し、復帰後に Router が再 enqueue する。
- `human_review_required` や `needs_fix` から `/retry` で `implementing` へ戻る際、Router は `branch_waiters` に `queued` として再 enqueue する。
- queue 先頭更新後、Router が次 task を dispatch する。

### 5.5 `/unlock`

主要受益ペルソナ: P2

- `/unlock <branch>` は admin only の強制 lease 解放操作である。
- 実装は `internal.unlock_requested` event として journal に append する。
- Router は対象 branch の Redis lease key を compare-and-delete し、`branch_waiters` の先頭 task を `queued` に戻して dispatch を再評価する。
- `branch_tokens.last_token` は巻き戻さない。次回 lease 取得時に新しい token を採番する。
- まだ生きている subprocess があれば、次回 renew / assert で失敗し親プロセスが停止させる。

---

## 6. Agent adapter 契約詳細

主要受益ペルソナ: P1, P3

### 6.1 Planner 出力

主要受益ペルソナ: P1, P3

- `goal`
- `sub_tasks[]`
- `allowed_files[]`
- `auto_allowed_patterns[]`
- `acceptance_criteria[]`
- `forbidden_changes[]`
- `risk_notes[]`

`auto_allowed_patterns` 例:

- `**/package-lock.json`
- `**/poetry.lock`
- `**/.pytest_cache/**`
- formatter により発生する repo policy 上のファイル

### 6.2 Plan Validator

主要受益ペルソナ: P1, P3

自動承認条件:

- `validator_errors = 0`
- `validator_score >= 85`
- `critical = false`
- `allowed_files` または `auto_allowed_patterns` が空でない
- `acceptance_criteria` が空でない

`validator_score` は rule-based の決定的スコアとし、LLM self-score を使わない。実装は浮動小数ではなく整数 0..100 で行い、閾値は `>= 85` とする。

| ルール | 配点 |
|---|---|
| `goal` が空でない | 10 |
| `sub_tasks[]` が 1 件以上で、各要素が具体動詞を含む | 15 |
| `allowed_files[] ∪ auto_allowed_patterns[]` が空でない | 20 |
| `acceptance_criteria[]` が 1 件以上で、各要素が観測可能条件を含む | 25 |
| `forbidden_changes[]` が 1 件以上 | 10 |
| `risk_notes[]` が 1 件以上 | 10 |
| schema validation 完全一致 | 10 |

計算式:

```
validator_score = Σ(満たした rule の配点)
```

`validator_errors` は hard error の件数であり、`validator_score` と独立に計上する。

Planner / Reviewer adapter metadata:

- `supports_request_id: bool`

### 6.3 Execution 境界検査

主要受益ペルソナ: P1, P2, P3

開始前:

- branch lease 保有確認
- `plan_rev` 一致確認
- `allowed_files` と `auto_allowed_patterns` を runner に注入

完了後:

- `changed_files` が `allowed_files ∪ auto_allowed_patterns` の範囲に収まるか判定
- 範囲外があれば `needs_fix`
- reviewer には「範囲外変更が実害を持つか」の判定も要求する
- executor 完了時、ToolRunner 親プロセスは `changed_files`, `exit_code`, `failure_code`, `last_known_head_commit` を含む `internal.executor_finished` event を journal に append する
- `reviewing` / `needs_fix` / `human_review_required` への遷移判定は Router が行う

glob 展開規則:

- `**`, `*`, `?` は `.gitignore` 非依存で評価する
- symlink は追跡しない
- 大文字小文字は実行ホストの filesystem semantics に従う
- path 正規化後に repo root 外へ出るものは即 reject する

### 6.4 Reviewer 出力

主要受益ペルソナ: P3

Reviewer は acceptance criterion ごとに以下を返す:

- `criterion_id`
- `status`: `satisfied` / `unsatisfied` / `unchecked`
- `evidence_refs[]`
  - diff hunk 参照
  - test 名
  - log path
- `notes`

全体出力:

- `decision`: `pass` / `fail` / `human_review_required`
- `criteria[]`
- `policy_breaches[]`
- `extra_files[]`

`evidence_refs` が空なら自動的に `unchecked` とみなす。`unchecked` が 1 件でもあれば `pass` 不可。

`policy_breaches[]` enum:

- `touched_forbidden_file`
- `touched_out_of_scope_file`
- `missing_test`
- `acceptance_not_met`
- `lease_assert_missing`
- `unexpected_generated_file`

---

## 7. ToolRunner とログ保持

主要受益ペルソナ: P2, P3, P4

### 7.1 ToolRunner

主要受益ペルソナ: P2, P3

- 親プロセスが subprocess を起動する。
- 親の責務: heartbeat renew task、stdout/stderr stream 受信、timeout 監視、kill / cleanup。
- Router は state 変更 commit 後、best-effort で Redis pub/sub `task_state_changed:<task_id>` を publish する。
- publish が失敗しても truth は SQLite にあり、親プロセス polling fallback が最終検知経路になる。

### 7.2 ログ保持と retention worker

主要受益ペルソナ: P3, P4

- 生イベントは `logs/<task_id>/<stage>/<started_at>_<call_id>.jsonl.zst` に追記する。
- SQLite `tool_calls` には `log_path`, `log_sha256`, `log_bytes` だけ保存する。
- `task-relay-retention.service` は単一 binary とし、log retention と journal retention の 2 schedule を持つ。
- retention worker を 1 日 1 回実行する。
- 保持期間:
  - full log 30 日
  - digest / metadata 180 日

retention worker の責務:

- 30 日超の log file に対応する `tool_calls.log_path`, `log_sha256`, `log_bytes` を先に null 化する。
- null 化 commit 後に log file を削除する。
- 180 日超の `tool_calls` metadata を削除する。
- journal retention は別 worker で実施し、30 日超ファイルを rotate 済みディレクトリから削除する。
- retention worker の初回実行時と起動時 sweep で、`tool_calls` と実体 file の整合を確認する。
- `log_path` が null で実体 file が残っていれば orphan file として削除し、`system_events` に `event_type=retention_orphan_detected`, `severity=warning` を append する。`payload_json` には少なくとも `orphan_kind=file`, `path`, `detected_by=retention_sweep` を含める。
- `log_path` が非 null で実体 file が無ければ metadata stale として null 化し、`system_events` に `event_type=retention_orphan_detected`, `severity=warning` を append する。`payload_json` には少なくとも `orphan_kind=metadata`, `call_id`, `path`, `detected_by=retention_sweep` を含める。
- router 稼働中に retention を実行してよい。grace 120 秒と保持 30 日の差が大きく、runbook 上の drain 条件は不要とする。

### 7.3 failure_code enum

主要受益ペルソナ: P2, P3

- `auth_error`
- `permission_error`
- `rate_limited`
- `network_unreachable`
- `timeout`
- `oom_killed`
- `invalid_plan_output`
- `invalid_review_output`
- `adapter_parse_error`
- `tool_internal_error`
- `system_degraded`

---

## 8. Failure Classification / Retry / Breaker

主要受益ペルソナ: P2

### 8.1 分類規則

主要受益ペルソナ: P2, P3

| failure_code | class | 自動再試行 |
|---|---|---|
| `rate_limited` | TRANSIENT | あり |
| `network_unreachable` | TRANSIENT | あり |
| `timeout` | UNKNOWN | `planning` / `reviewing` のみ 1 回。provider request id を送れない場合はなし |
| `oom_killed` | UNKNOWN | `planning` / `reviewing` のみ 1 回。`executing` はなし |
| `invalid_plan_output` | UNKNOWN | repair retry 2 回まで |
| `invalid_review_output` | UNKNOWN | repair retry 1 回まで |
| `auth_error` | FATAL | なし |
| `permission_error` | FATAL | なし |
| `adapter_parse_error` | FATAL | なし |
| `tool_internal_error` | UNKNOWN | 1 回 |
| `system_degraded` | FATAL | なし |

idempotency 規則:

- Planner / Reviewer adapter は provider が対応する場合、client 生成 `request_id` を header または request field に必ず送る。
- `timeout` 再試行は同一 `request_id` を継続利用できる場合に限る。
- Execution stage は workspace / Git mutation を含むため、`timeout` / `oom_killed` の自動再試行を禁止する。
- Execution stage の `timeout` / `oom_killed` は reconcile 後に `implementing_resume_pending` または `human_review_required` に送る。
- `supports_request_id = false` の adapter では `planning` / `reviewing` の `timeout` 自動再試行を禁止する。
- `supports_request_id = false` の adapter で `planning` timeout が起きた場合は `human_review_required` とする。
- `supports_request_id = false` の adapter で `reviewing` timeout が起きた場合も `human_review_required` とする。

### 8.2 repair retry

主要受益ペルソナ: P2

- `invalid_plan_output` 再試行時は system prompt に以下を追加:
  - `前回出力は JSON 契約を満たしていない。意味内容を保ったまま有効な JSON のみを返せ`
- 3 回連続で壊れた場合に初めて `human_review_required`。

### 8.3 Circuit Breaker

主要受益ペルソナ: P2

- breaker 集計キーは `failure_code` 単位の global 集計。
- 同一 `failure_code` の FATAL が 10 分以内に 3 件で open。
- open 中は新規 dispatch を停止する。
- 既存 in-flight task は kill しない。完走または個別 failure まで継続させる。
- reset 操作:
  - `runner-cli retry-system --stage <stage>`
  - Discord `/retry-system stage:<stage>` は admin allowlist user のみ
- `retry-system` の `stage` 引数は health check と dispatch resume 対象を指定する。
- breaker 自体は全 stage 共有の `failure_code` 集計を reset する。`stage` 引数は auxiliary であり、breaker reset 自体は常に global である。
- health check 成功後、`retry-system` は `internal.system_recovered` event を journal に append する。

---

## 9. Projection 実装詳細

主要受益ペルソナ: P3

### 9.1 stream の種類と payload

主要受益ペルソナ: P3

- `task_snapshot`
  - 現在 state, state_rev, plan_rev, critical, URLs
  - 古いものは新しい `state_rev` によって supersede 可
  - Forgejo issue body / frontmatter のみを管理し、label は管理しない
  - frontmatter には少なくとも `state`, `state_rev`, `plan_rev`, `critical`, `task_url` を含める
  - body には plan 本文や diff を書かない
- `task_comment`
  - 監査コメント
  - FIFO 必須
- `task_label_sync`
  - `critical`, `human_review_required`, `cancelled` など allowlist label を Forgejo label API と同期する
  - payload は「追加差分」ではなく allowlist label の desired set 全体を持つ
  - Forgejo labels を単独管理する
  - FIFO 必須
- `discord_alert`
  - DM 通知
  - FIFO 必須

### 9.2 順序規則

主要受益ペルソナ: P3

- worker は `(task_id, stream, target)` ごとに `outbox_id` 昇順で処理する。
- `(task_id, stream, target)` について同時に 1 worker しか in-flight を持ってはならない。
- claim は SQLite transaction で最古の未送信行に `claimed_by`, `claimed_at` を設定して行う。
- `task_snapshot` は送信前に `projection_cursors` を参照し、`state_rev <= last_sent_state_rev` なら drop する。
- `task_snapshot` は送信成功時のみ `projection_cursors.last_sent_state_rev` を更新する。
- `task_comment` と `task_label_sync` は drop 禁止。
- remote mirror の frontmatter は順序判定に使わない。
- remote 読み取りは rebuild と「送信済みか不明な comment / alert の dedup」の補助用途に限定する。

運用上の前提:

- remote dedup path は `sent_at` 更新前クラッシュなどの異常回復時だけに通る。通常 steady state の hot path ではない。
- issue は task ごとに 1 つを前提とし、relay 生成 comment 数の運用上限は 500 件とする。
- dedup 時は最新 200 件を先に走査し、marker が見つからない場合のみ上限 500 件まで full scan する。

### 9.3 retry policy

主要受益ペルソナ: P2, P3

- 初回 1 分。
- 以後指数バックオフ、上限 1 時間。
- 停止条件は OR:
  - 24 時間送れなければ `system_degraded`
  - または retry 回数が 50 回に達したら `system_degraded`

例外:

- `done` と `cancelled` task の projection 永久失敗では task state を変更しない。
- その場合は `system_events` に error を記録し、運用監視で拾う。

### 9.4 projection rebuild

主要受益ペルソナ: P3, P4

- `runner-cli projection-rebuild --task <task_id>`
- 目的は DB 欠損後の outbox 補完であり、既送信 remote mirror の強制再生成ではない。
- デフォルト実装は `INSERT ... ON CONFLICT DO NOTHING` とし、欠損行だけを補う。
- `--force` 指定時のみ対象 task の `sent_at IS NULL` な outbox 行を削除してから再生成する。
- `--force` でも `sent_at IS NOT NULL` な `task_comment` / `discord_alert` は削除しない。
- 最新 SQLite 真実から snapshot / comment / label outbox を再生成する。
- rebuild 時のみ mirror を参照して remote 現況との差分確認をしてよい。

---

## 10. レート制御

主要受益ペルソナ: P1

### 10.1 API 系

主要受益ペルソナ: P1

- 応答ヘッダに `remaining`, `reset_at` があればそれを真とする。
- SQLite 更新成功後に Redis cache を更新する。

### 10.2 Subscription CLI 系

主要受益ペルソナ: P1

ヘッダが無い場合の規則:

1. その日の最初の使用時刻を UTC で `window_started_at` に保存する。
2. `window_reset_at = window_started_at + 5h`。
3. 429 相当を検知したら `remaining = 0` とし、`window_reset_at` まで停止する。
4. `now >= window_reset_at` で window を再開始する。

根拠:

- subscription 提供側の quota window は通常 UTC 系で管理され、ホストのローカル TZ に依存しないため。

### 10.3 受付停止

主要受益ペルソナ: P1, P2

`limit = 0` は「未観測」を意味し、停止判定に使ってはならない。

```python
if limit > 0 and remaining < limit * 0.2:
    stop_new_tasks = True
```

補足:

- v1.0 で確定するのは内部保護としての受付停止規則までであり、P1 向けの cost / rate 可視化や通知は本書の正本範囲外とする。
- `tool_calls.tokens_in`, `tokens_out` は将来の使用量集計の素材であり、v1.0 では集計 window、alert threshold、通知頻度を確定しない。

---

## 11. Cold Start / Reconcile

主要受益ペルソナ: P2, P4

### 11.1 起動順

主要受益ペルソナ: P2, P4

```
1. redis.service
2. forgejo.service
3. task-relay-db-check.service
4. task-relay-journal-replay.service
5. task-relay-journal-ingester.service
6. task-relay-reconcile.service
7. task-relay-router.service
8. task-relay-discord-bot.service
9. task-relay-projection.service
10. task-relay-retention.service
```

`task-relay-journal-replay.service` は起動時 replay 専用、`task-relay-journal-ingester.service` は継続 ingest 専用とする。
ingester 再開位置は `journal_ingester_state(last_file, last_offset)` を一次情報とする。

- `task-relay-reconcile.service` は直接 state を更新せず、`internal.reconcile_resume` event を journal に append する。
- Router 起動後にその internal event を処理して状態復元を行う。

### 11.2 `implementing` の再開規則

主要受益ペルソナ: P2, P4

起動時に `implementing` task を一律人間送りにはしない。

| 条件 | 処理 |
|---|---|
| worktree clean | `plan_approved` に戻し、自動で lease 再取得を試みる |
| worktree dirty かつ最終 heartbeat が停止前 60 秒以内、同一 `task_id + plan_rev` | `implementing_resume_pending` に遷移し、120 秒の grace window で同 task のみ再開可 |
| 上記以外 | `human_review_required` |

再開前に確認すること:

- branch HEAD が `tasks.last_known_head_commit` から進んでいない
- `plan_rev` が一致する
- 変更ファイルが `allowed_files ∪ auto_allowed_patterns` に収まる

`tasks.last_known_head_commit` は各 Git mutate 成功直後に ToolRunner 親プロセスが更新していることを前提とする。

### 11.3 可視化

主要受益ペルソナ: P2, P3

- `runner-cli reconcile-report --last`
- `/status` でも直近 reconcile の件数を表示

---

## 12. 認証・権限詳細

主要受益ペルソナ: P1, P2, P4

### 12.1 Secret redact allowlist

主要受益ペルソナ: P2, P4

適用範囲: log / stderr / trace / system comment など観測用出力。`event_inbox.payload_json` / `projection_outbox.payload_json` は真実源として保持する。

記録してよい key pattern:

- `task_id`
- `issue_id`
- `issue_number`
- `state`
- `state_rev`
- `plan_rev`
- `failure_code`
- `request_id`
- `remaining`
- `limit`

記録してはならないもの:

- `authorization`
- `cookie`
- `token`
- `secret`
- `private_key`
- message content 全文

### 12.2 管理コマンド matrix

主要受益ペルソナ: P1, P2, P4

| コマンド | 権限 |
|---|---|
| `/approve` | `task.requested_by` または `admin_user_ids` |
| `/critical` | `task.requested_by` または `admin_user_ids` |
| `/retry` | `task.requested_by` または `admin_user_ids` |
| `/cancel` | `task.requested_by` または `admin_user_ids` |
| `/unlock` | `admin_user_ids` のみ |
| `/retry-system` | `admin_user_ids` のみ |

---

## 13. Backup / DR / SQLite 運用

主要受益ペルソナ: P4

### 13.1 方式

主要受益ペルソナ: P4

- Litestream continuous replication to S3-compatible storage
- 日次 SQLite snapshot を別物理ディスクへ
- journal は 30 日保持、7 日はローカル + オフサイト二重化

### 13.2 成功判定

主要受益ペルソナ: P4

- `replication_lag_seconds <= 60`
- 前回 restore drill 成功
- 最新 snapshot 24 時間以内
- journal sync lag 60 秒以内

restore drill 頻度: 四半期ごとに 1 回以上。

### 13.3 WAL / VACUUM

主要受益ペルソナ: P4

- WAL auto-checkpoint: 256 MiB 超で実施
- 週次 `wal_checkpoint(TRUNCATE)`
- 月次 `VACUUM`

### 13.4 Restore 手順

主要受益ペルソナ: P4

1. 最新 Litestream replica から復元する。
2. 復元済み SQLite の `journal_ingester_state` を開始点とし、無い場合は `max(event_inbox.journal_offset)` を開始点として journal replay する。
3. reconcile を実行する。
4. projection rebuild する。
5. health check を実行する。

`journal_ingester_state` と `event_inbox.journal_offset` がずれていても、`unique(source, delivery_id)` により replay は冪等である。

---

## 14. モバイル UX

主要受益ペルソナ: P1, P2

- Discord は task 投入と簡易確認のみ。
- `/status` を P2 の主運用ビューとし、少なくとも `局所障害`, `全体障害`, `全体保護中(dispatch pause)` の scope ラベルを返す。
- 詳細は Tailscale 越し Forgejo Web。
- Discord に出す情報: task ID / 状態 / 件数 / Forgejo URL。
- 出してはいけない情報: plan 本文 / diff / log 詳細 / secret / cost 明細。

---

## 15. 関連ドキュメント

- `basic-design-v1.0.md` (本書と対になる基本設計)
- `docs/reference/schema.md`
- `docs/reference/state-machine.md`
- `docs/reference/runbook.md`
- `docs/reference/disaster-recovery.md`
- `docs/reference/reconcile.md`
- `docs/reference/failure-injection.md`
- `.versions.yaml`
