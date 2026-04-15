# task-relay State Machine Reference

出典:
- [basic-design-v1.0.md](../../basic-design-v1.0.md)
- [detailed-design-v1.0.md](../../detailed-design-v1.0.md)

本書は task state machine をまとめた参照ビューである。
実装仕様の source of truth は `detailed-design-v1.0.md` §4.3 および `basic-design-v1.0.md` §6 であり、本書はそれらを運用者とレビューア向けに引きやすく整理したものに過ぎない。
矛盾時は `detailed-design-v1.0.md` と `basic-design-v1.0.md` が勝つ。

## 1. 不変条件

- `tasks.state`, `state_rev`, `critical`, `manual_gate_required`, `resume_target_state` は Router だけが更新する。
- 外部入力も internal 制御イベントも `journal -> inbox -> Router` を通る。
- self-loop や flag 変更のみの遷移でも `state_rev` は 1 増やす。
- `system_degraded` は新規 dispatch 停止と system-wide 異常を表す。task 局所停止と混同しない。

## 2. 状態一覧

| State | 意味 |
|---|---|
| `new` | 受理直後。まだ planning 未着手 |
| `planning` | Planner 実行中 |
| `plan_pending_approval` | plan はあるが人間承認待ち |
| `plan_approved` | plan は承認済みで dispatch 待ち |
| `implementing` | Executor 実行中 |
| `implementing_resume_pending` | 再起動後の短時間再開待ち |
| `needs_fix` | 実装は終わったが再実装が必要 |
| `reviewing` | Reviewer 実行中 |
| `human_review_required` | 人間判断が必要な停止 |
| `done` | 完了 |
| `cancelled` | 明示キャンセル済み |
| `system_degraded` | system-wide 異常のため復旧待ち |

## 3. Trigger と internal event

主要 external trigger:

- task 作成
- `/approve`
- `/retry`
- `/retry --replan`
- `/cancel`
- `/critical on`

主要 internal event:

- `internal.executor_finished`
- `internal.planner_timeout`
- `internal.reviewer_timeout`
- `internal.lease_lost`
- `internal.infra_fatal`
- `internal.reconcile_resume`
- `internal.system_recovered`
- `internal.unlock_requested`

## 4. 正常系遷移

| From | Trigger | Guard | To |
|---|---|---|---|
| `new` | task 作成受理 | なし | `planning` |
| `planning` | planner が有効 plan を返す | auto approve 条件を満たす | `plan_approved` |
| `planning` | planner が有効 plan を返す | auto approve 条件を満たさない | `plan_pending_approval` |
| `plan_pending_approval` | `/approve` | `plan_rev` が現行一致 | `plan_approved` |
| `plan_approved` | dispatch 実行 | branch lease 取得成功 | `implementing` |
| `plan_approved` | dispatch 実行 | branch lease 未取得 | `plan_approved` |
| `implementing` | `internal.executor_finished` | `exit_code=0` かつ `changed_files` が許容範囲内 | `reviewing` |
| `reviewing` | reviewer `decision=pass` | `unchecked=0` かつ `manual_gate_required=false` | `done` |
| `reviewing` | reviewer `decision=pass` | `unchecked=0` かつ `manual_gate_required=true` | `human_review_required` |

## 5. 例外系遷移

| From | Trigger | Guard | To |
|---|---|---|---|
| `planning` | validator failure 規定回数超過 | なし | `human_review_required` |
| `planning` | `internal.planner_timeout` | なし | `human_review_required` |
| `planning` | `internal.infra_fatal` / breaker open | なし | `system_degraded` |
| `plan_pending_approval` | `/critical on` | `critical=false` | `plan_pending_approval` |
| `plan_pending_approval` | `/retry --replan` | なし | `planning` |
| `plan_pending_approval` | `/cancel` | なし | `cancelled` |
| `plan_approved` | `/critical on` | なし | `plan_pending_approval` |
| `implementing` | `internal.executor_finished` | 範囲外変更あり | `needs_fix` |
| `implementing` | `internal.executor_finished` | infra ではない一般エラー | `needs_fix` |
| `implementing` | `internal.reconcile_resume` | dirty worktree かつ再開条件を満たす | `implementing_resume_pending` |
| `implementing` | `internal.lease_lost` | なし | `human_review_required` |
| `implementing` | `internal.infra_fatal` / breaker open | なし | `system_degraded` |
| `implementing_resume_pending` | 同一 task の再開要求 | HEAD / `plan_rev` / allowed_files 一致 | `implementing` |
| `implementing_resume_pending` | 120 秒経過 | なし | `human_review_required` |
| `needs_fix` | `/retry` | 同一 `plan_rev` 継続 | `implementing` |
| `needs_fix` | `/retry --replan` | なし | `planning` |
| `reviewing` | reviewer `decision=fail` | なし | `needs_fix` |
| `reviewing` | reviewer `decision=human_review_required` | なし | `human_review_required` |
| `reviewing` | `internal.reviewer_timeout` | なし | `human_review_required` |
| `human_review_required` | `/approve` | review 済みかつ `manual_gate_required=true` | `done` |
| `human_review_required` | `/retry` | 実装再試行 | `implementing` |
| `human_review_required` | `/retry --replan` | 再計画 | `planning` |
| `system_degraded` | `internal.system_recovered` | root cause 解消かつ `resume_target_state` 非 null | `resume_target_state` を再評価 |
| `*` | `/cancel` | `done` 以外 | `cancelled` |

## 6. `system_degraded` 復帰規則

- `* -> system_degraded` では、Router は現在 state を `resume_target_state` に退避してから `state=system_degraded` に更新する。
- 復帰 transaction では `resume_target_state` を clear する。
- 復帰時は退避先 state の進入 guard を再評価する。
- 復帰先が `implementing` の場合、lease と subprocess 継続前提は失われているため `plan_approved` にフォールバックし、wait queue に再 enqueue する。
- 復帰先が `reviewing` の場合、以前の reviewer 結果は再利用せず reviewer を再 dispatch する。
- 復帰先が dispatchable でない、または health check を満たさない場合は `human_review_required` に送る。
- `done` と `cancelled` は projection 永久失敗だけを理由に `system_degraded` へ送らない。
- `lease_branch` 直列化は relay-managed publish 順序を守るためのものであり、human merge の安全性保証ではない。

## 7. `/critical` 規則

| 現状態 | `/critical on` の結果 |
|---|---|
| `new`, `planning`, `plan_pending_approval` | `critical=true`。以後 auto approve 無効 |
| `plan_approved` | `critical=true` にして `plan_pending_approval` へ戻す |
| `implementing` | `critical=true`, `manual_gate_required=true`。実装は継続するが自動完了は禁止 |
| `reviewing` | `critical=true`, `manual_gate_required=true` |
| `done` | 監査のみ |

`/critical off` 規則:

- 自動で false にしない。
- `task.requested_by` または `admin_user_ids` の明示操作でのみ成立する。
- `manual_gate_required` は暗黙に解除しない。

## 8. operator 向け参照

- operator 向け scope ラベルの source of truth は [runbook.md](runbook.md) §1.1 とする。
- 本書は state machine 自体を扱い、`局所障害 / 局所停止 / 人間待ち / 全体障害 / 全体保護中` の表示語彙は runbook に委譲する。
- 深夜対応の次アクション判断は [runbook.md](runbook.md) と [../guides/ops-cards/system-degraded.md](../guides/ops-cards/system-degraded.md), [../guides/ops-cards/human-review-required.md](../guides/ops-cards/human-review-required.md) を参照する。

補足:

- `breaker open` は task state ではないため、本書の状態一覧や遷移表には載せない。
- ただし operator 視点では `system_degraded` と並んで重要な全体影響であり、scope ラベルは `全体保護中` として扱う。
- P2 が状態機械から運用判断へ進む場合、`system_degraded` だけでなく `breaker open` も [runbook.md](runbook.md) §1.1 の scope 表で確認する。

## 9. 実装メモ

- ToolRunner 親は直接 state を変えない。`internal.executor_finished` や `internal.lease_lost` を journal に append する。
- timeout 自動再試行不可時、ToolRunner 親は `internal.planner_timeout` / `internal.reviewer_timeout` を append する。
- `/unlock` は branch lease 制御イベントであり、通常は task state を直接変えない。
- `reviewing -> done` 判定に `pull_request` Webhook は使わない。

## 10. 関連ドキュメント

- [runbook.md](runbook.md)
- [reconcile.md](reconcile.md)
- [schema.md](schema.md)
