# task-relay Failure Injection Canonical

出典:
- [basic-design-v1.0.md](../../basic-design-v1.0.md)
- [detailed-design-v1.0.md](../../detailed-design-v1.0.md)

本書は設計不変条件を壊していないことを検証するための failure injection 計画書である。
ユニットテストの一覧ではなく、障害モードごとに何を壊し、何が守られるべきかを定義する。

## 1. 目的

- commit 境界、冪等性、停止伝播、reconcile、DR の契約を検証する
- P2 の「深夜でも判断できること」と P4 の「反復可能な保守」を壊していないことを確認する
- implementation が詳細設計から逸脱していないことを見つける

## 2. 共通合格条件

- task truth が SQLite で一貫している
- Redis や Forgejo だけにしか存在しない重要状態が生まれない
- `cancelled` 後に subprocess が継続しない
- projection 再送で comment / alert が不必要に二重化しない
- restore 後に replay / reconcile / rebuild が完走する

## 3. 優先シナリオ

### 3.1 Ingress / Journal

対応する ops-card:
- [../guides/ops-cards/human-review-required.md](../guides/ops-cards/human-review-required.md)

| 注入点 | 期待結果 |
|---|---|
| journal append 前に失敗 | 受理しない。ACK しない |
| append 後、ingest 前に停止 | replay 後に inbox へ取り込まれる |
| Discord 1500ms 超過 | `request_id` のみ返し、受理しない |

### 3.2 Router Transaction

対応する ops-card:
- [../guides/ops-cards/post-incident-audit.md](../guides/ops-cards/post-incident-audit.md)

| 注入点 | 期待結果 |
|---|---|
| outbox INSERT 後、commit 前クラッシュ | `processed_at` も outbox も残らない |
| commit 直後クラッシュ | state / outbox / processed_at が一貫して残る |
| 同一 event 再実行 | 決定的 payload と同じ outbox 意味論を再現する |

### 3.3 Projection

対応する ops-card:
- [../guides/ops-cards/post-incident-audit.md](../guides/ops-cards/post-incident-audit.md)

| 注入点 | 期待結果 |
|---|---|
| remote 送信成功後、`sent_at` 更新前クラッシュ | remote dedup で再投稿を防ぎ、`sent_at` を補完する |
| `task_snapshot` rebuild | payload-sensitive identity により補完と supersede が両立する |
| retry 上限到達 (`done`, `cancelled`) | task state を巻き戻さず `system_events` に記録する |

### 3.4 ToolRunner / Lease

対応する ops-card:
- [../guides/ops-cards/system-degraded.md](../guides/ops-cards/system-degraded.md)
- [../guides/ops-cards/human-review-required.md](../guides/ops-cards/human-review-required.md)

| 注入点 | 期待結果 |
|---|---|
| renew 2 回連続失敗 | 親が subprocess を停止し、`internal.lease_lost` を append する |
| `cancelled` commit 後の通知喪失 | 親の polling fallback で停止を検知する |
| Git mutate 前の token 不一致 | mutate を開始しない |
| `feature_branch` push 前の lease 欠落 | remote publish を開始しない |

### 3.5 Timeout event formalization

対応する ops-card:
- [../guides/ops-cards/human-review-required.md](../guides/ops-cards/human-review-required.md)

| 注入点 | 期待結果 |
|---|---|
| planner timeout, `supports_request_id=false` | 親が `internal.planner_timeout` を append し Router が `human_review_required` に送る |
| reviewer timeout, retry budget 消費済み | 親が `internal.reviewer_timeout` を append し Router が `human_review_required` に送る |
| timeout event payload 欠損 | 楽観遷移せず `human_review_required` と監査可能 payload を優先する |

### 3.6 Reconcile / Restart

対応する ops-card:
- [../guides/ops-cards/system-degraded.md](../guides/ops-cards/system-degraded.md)
- [../guides/ops-cards/human-review-required.md](../guides/ops-cards/human-review-required.md)

| 注入点 | 期待結果 |
|---|---|
| `implementing` 中にプロセス停止、worktree clean | `plan_approved` に戻す |
| `implementing` 中に停止、dirty かつ条件一致 | `implementing_resume_pending` に入る |
| `implementing_resume_pending` で 120 秒経過 | `human_review_required` |

### 3.7 Disaster Recovery

対応する ops-card:
- [../guides/ops-cards/restore-drill.md](../guides/ops-cards/restore-drill.md)

| 注入点 | 期待結果 |
|---|---|
| SQLite 消失、replica あり | restore -> replay -> reconcile -> rebuild で復旧できる |
| `journal_ingester_state` 欠損 | `max(event_inbox.journal_offset)` から replay できる |
| restore 後の rebuild | 欠損補完として完走する |
| offsite journal lag > 60s | restore drill script が非 0 exit で失敗を返す |

## 4. 実施タイミング

- 大きな状態機械変更前後
- projection / retention / reconcile の実装変更後
- quarterly restore drill の前後
- release candidate 作成前

## 5. 記録項目

- 実施日
- 対象 build / commit
- 注入した障害
- 期待結果
- 実結果
- 差分
- 要修正項目

## 6. 関連ドキュメント

- [state-machine.md](state-machine.md)
- [runbook.md](runbook.md)
- [disaster-recovery.md](disaster-recovery.md)
