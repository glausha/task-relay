# task-relay Schema Reference

出典:
- [basic-design-v1.0.md](../../basic-design-v1.0.md)
- [detailed-design-v1.0.md](../../detailed-design-v1.0.md)

本書は SQLite と補助ファイルの責務境界をまとめた参照ビューである。
実装仕様の source of truth は `detailed-design-v1.0.md` §2 および `basic-design-v1.0.md` §3.2 であり、本書はそれらを引きやすく整理したものに過ぎない。
矛盾時は `detailed-design-v1.0.md` と `basic-design-v1.0.md` が勝つ。

## 1. 目的

- task truth と operational metadata を分離する。
- `journal -> inbox -> Router` と `projection_outbox -> external` の境界を明確にする。
- 保守、監査、rebuild、retention の対象を曖昧にしない。

## 2. データ分類

### 2.1 Truth

Router だけが更新できる task truth:

- `tasks.state`
- `tasks.state_rev`
- `tasks.critical`
- `tasks.manual_gate_required`
- `tasks.resume_target_state`
- `plans`

### 2.2 Event / Projection

イベントと外部反映のキュー:

- `event_inbox`
- `projection_outbox`
- `projection_cursors`

### 2.3 Branch / Dispatch Control

branch 排他と dispatch 制御:

- `branch_waiters`
- `branch_tokens`

### 2.4 Operational Metadata

task truth ではないが運用に必要な情報:

- `tasks.last_known_head_commit`
- `tool_calls`
- `rate_windows`
- `journal_ingester_state`
- `system_events`

## 3. 主テーブル

### 3.1 `tasks`

役割:
- task の現在地と truth flag を保持する。

主要カラム:
- `task_id`
- `state`
- `state_rev`
- `critical`
- `manual_gate_required`
- `current_branch`
- `last_known_head_commit`
- `resume_target_state`
- `requested_by`
- `created_at`
- `updated_at`

規則:
- `state` と truth flag は Router だけが更新する。
- `last_known_head_commit` は ToolRunner 親が Git mutate 成功直後に更新する。
- `resume_target_state` は `system_degraded` 遷移時に退避し、復帰時に clear する。

### 3.2 `plans`

役割:
- `plan_rev` ごとの plan と承認監査を保持する。

主要カラム:
- `task_id`
- `plan_rev`
- `planner_version`
- `plan_json`
- `validator_score`
- `validator_errors`
- `approved_by`
- `approved_at`
- `approved_kind`
- `created_at`

### 3.3 `event_inbox`

役割:
- Router が唯一読む durable event queue。

主要カラム:
- `event_id`
- `source`
- `delivery_id`
- `event_type`
- `payload_json`
- `journal_offset`
- `received_at`
- `processed_at`

制約:
- `unique(source, delivery_id)`

### 3.4 `projection_outbox`

役割:
- Forgejo / Discord への外部反映を outbox パターンで管理する。

主要カラム:
- `outbox_id`
- `task_id`
- `stream`
- `target`
- `origin_event_id`
- `payload_json`
- `state_rev`
- `idempotency_key`
- `claimed_by`
- `claimed_at`
- `attempt_count`
- `next_attempt_at`
- `sent_at`

制約:
- `unique(stream, target, idempotency_key)`

### 3.5 `projection_cursors`

役割:
- `task_snapshot` の supersede と送達進捗を記録する。

主要カラム:
- `task_id`
- `stream`
- `target`
- `last_sent_state_rev`
- `last_sent_outbox_id`
- `updated_at`

### 3.6 `branch_waiters`

役割:
- branch ごとの dispatch 待機列の真実源。

主要カラム:
- `branch`
- `task_id`
- `queue_order`
- `status`

規則:
- `queue_order` は SQLite 単調増加 INTEGER を使う。
- `status` enum は `queued`, `leased`, `paused_system_degraded`, `removed`。

### 3.7 `branch_tokens`

役割:
- fencing token の単調増加を担保する。

主要カラム:
- `branch`
- `last_token`

### 3.8 `journal_ingester_state`

役割:
- 継続 ingester の再開位置を保持する。

主要カラム:
- `singleton_id`
- `last_file`
- `last_offset`
- `updated_at`

規則:
- 常に 1 行だけ持つ。
- `event_inbox` 反映 transaction と同時に更新する。

### 3.9 `tool_calls`

役割:
- ToolRunner 実行の運用記録と log 参照を保持する。

主要カラム:
- `call_id`
- `task_id`
- `stage`
- `tool_name`
- `started_at`
- `ended_at`
- `duration_ms`
- `success`
- `exit_code`
- `failure_code`
- `log_path`
- `log_sha256`
- `log_bytes`
- `tokens_in`
- `tokens_out`

### 3.10 `rate_windows`

役割:
- provider / subscription の rate 観測値を保持する。

主要カラム:
- `tool_name`
- `window_started_at`
- `window_reset_at`
- `remaining`
- `limit`
- `updated_at`

### 3.11 `system_events`

役割:
- state 変更以外の運用イベントを append-only で残す。

主要カラム:
- `task_id`
- `event_type`
- `severity`
- `payload_json`
- `created_at`

代表的な `event_type`:
- `mirror_readonly_violation_detected`
- `retention_orphan_detected`

## 4. 補助ファイル

SQLite に全文を持たない補助ファイル:

- `var/task-relay/journal/YYYYMMDD.ndjson.zst`
- `var/task-relay/logs/<task_id>/<stage>/<started_at>_<call_id>.jsonl.zst`

規則:
- journal は受理前耐久化の正本であり、日単位 rotate + zstd 圧縮とする。
- tool log は file に保持し、SQLite には path / hash / size だけを保持する。
- `raw_events` を SQLite に二重保持しない。

## 5. 更新権限

### 5.1 Router

更新できるもの:
- `tasks.state`, `state_rev`, truth flag
- `plans`
- `event_inbox.processed_at`
- `projection_outbox` insert
- `branch_waiters`
- `system_events`

### 5.2 ToolRunner 親

更新できるもの:
- `tool_calls`
- `tasks.last_known_head_commit`
- `rate_windows`
- `system_events`

更新してはならないもの:
- `tasks.state`
- `tasks.state_rev`
- truth flag

### 5.3 Projection Worker

更新できるもの:
- `projection_outbox.sent_at`, retry 系カラム
- `projection_cursors`
- `rate_windows`
- `system_events`

### 5.4 Journal Ingester / Reconcile / Retention

- Journal Ingester は `event_inbox` insert と `journal_ingester_state` 更新のみ。
- Reconcile Worker は直接 state を更新せず、internal event を journal に append する。
- Retention Worker は `tool_calls` metadata null 化 / GC と、orphan 検出時の `system_events` append を行う。

## 6. 主要制約

- SQLite だけが truth であり、Forgejo や Redis は真実源ではない。
- Router transaction は `outbox INSERT` と `event_inbox.processed_at` を同一 commit に入れる。
- Projection は決定的 `idempotency_key` を持つ。
- `task_snapshot` の identity は `state_rev` 単独ではなく payload-sensitive である。
- retention は `tool_calls` metadata と file 実体の整合を保つ。

## 7. 関連ドキュメント

- [state-machine.md](state-machine.md)
- [runbook.md](runbook.md)
- [reconcile.md](reconcile.md)
- [disaster-recovery.md](disaster-recovery.md)
