# task-relay Reconcile Reference

出典:
- [basic-design-v1.0.md](../../basic-design-v1.0.md)
- [detailed-design-v1.0.md](../../detailed-design-v1.0.md)

本書は cold start 後の整合回復と resume 判定をまとめた参照ビューである。
実装仕様の source of truth は `detailed-design-v1.0.md` §11 であり、本書は運用参照のために内容を切り出したものに過ぎない。
矛盾時は `detailed-design-v1.0.md` が勝つ。

## 1. 目的

- 前回停止時の中途状態を安全に再解釈する
- `implementing` task を一律人間送りにせず、再開可能なものだけを再開候補にする
- stuck queue と長期 `system_degraded` を整理する

## 2. 起動順

標準起動順:

1. `task-relay-db-check.service`
2. `task-relay-journal-replay.service`
3. `task-relay-journal-ingester.service`
4. `task-relay-reconcile.service`
5. `task-relay-router.service`

原則:
- reconcile は Router より前に走ってよい
- ただし state を直接更新してはならない
- reconcile 結果は `internal.reconcile_*` event として journal に append し、Router 起動後に処理する

## 3. 入力

reconcile が参照するもの:

- `tasks`
- `plans`
- `branch_waiters`
- `tool_calls`
- `journal_ingester_state`
- workspace の clean / dirty 状態
- branch HEAD
- 最終 heartbeat 相当の fresheness

## 4. `implementing` 再開判定

### 4.1 判定表

| 条件 | Router に渡すべき遷移 |
|---|---|
| worktree clean | `plan_approved` へ戻し、自動で lease 再取得を試みる |
| worktree dirty かつ最終 heartbeat が停止前 60 秒以内、同一 `task_id + plan_rev` | `implementing_resume_pending` |
| 上記以外 | `human_review_required` |

### 4.2 再開前 guard

以下をすべて確認する:

- branch HEAD が `tasks.last_known_head_commit` から進んでいない
- `plan_rev` が一致する
- 変更ファイルが `allowed_files ∪ auto_allowed_patterns` に収まる

### 4.3 `implementing_resume_pending`

- grace は 120 秒
- 同一 task だけが再開できる
- grace 超過時は `human_review_required`

## 5. `system_degraded` と wait queue

- `system_degraded` は短期復帰前提のため、直ちに wait queue から除去しない
- 24 時間継続した task は reconcile が wait queue から除去する
- 復帰後の再実装が必要なら Router が再 enqueue する

## 6. event 生成規則

reconcile が append してよい event:

- `internal.reconcile_resume`

reconcile が直接やってはいけないこと:

- `tasks.state` 更新
- `state_rev` 更新
- truth flag 更新

補足:
- system-wide 復帰そのものは `internal.system_recovered` による
- lease 喪失は ToolRunner 親が `internal.lease_lost` を append する

## 7. 可視化

運用者が確認する経路:

- `runner-cli reconcile-report --last`
- `/status`

最低限見えるべき内容:

- 何件が `plan_approved` に戻ったか
- 何件が `implementing_resume_pending` に入ったか
- 何件が `human_review_required` に送られたか
- 何件が wait queue から整理されたか

## 8. 失敗時の扱い

- reconcile 自身の失敗は `system_events` に記録する
- reconcile 失敗だけで task truth を直接書き換えない
- resume 判定に必要な入力が欠ける場合は楽観再開せず、より保守的な遷移を選ぶ

## 9. 関連ドキュメント

- [state-machine.md](state-machine.md)
- [runbook.md](runbook.md)
- [disaster-recovery.md](disaster-recovery.md)
