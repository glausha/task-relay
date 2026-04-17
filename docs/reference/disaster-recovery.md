# task-relay Disaster Recovery Reference

出典:
- [basic-design-v1.0.md](../../basic-design-v1.0.md)
- [detailed-design-v1.0.md](../../detailed-design-v1.0.md)

本書は backup / restore / restore drill をまとめた参照ビューである。
実装仕様の source of truth は `detailed-design-v1.0.md` §13 と `basic-design-v1.0.md` §10 であり、本書は P4 が運用で引きやすい形に整理したものに過ぎない。
矛盾時は `detailed-design-v1.0.md` と `basic-design-v1.0.md` が勝つ。

## 1. 目標

- RPO 60 秒以内
- RTO 30 分以内
- journal 消失なし
- restore 後に reconcile と projection rebuild まで完了できること

## 2. 真実源と復旧対象

復旧の一次対象:

- SQLite WAL primary
- on-site Litestream replica bucket
- offsite replica bucket (MinIO bucket replication 先)
- 日次 snapshot
- ingress journal

復旧の補助対象:

- log file
- Forgejo mirror
- Redis cache

原則:
- SQLite が truth
- Redis は再構築対象
- Forgejo は mirror であり復旧元の真実源にしない

## 3. 通常時の成功判定

以下を満たしていること:

- `replication_lag_seconds <= 60`
- `snapshot_age_seconds <= 86400`
- `journal_offsite_lag_seconds <= 60`
- 前回 restore drill 成功
- `PRAGMA integrity_check = ok`

restore drill 頻度:
- 四半期ごとに 1 回以上

## 4. 保持方針

- SQLite は Litestream で継続レプリケーション
- Litestream の live replica は on-site MinIO bucket を 1 つだけ使う
- offsite 二重化は MinIO bucket replication で行う
- 日次 snapshot を別物理ディスクへ保持
- journal は 30 日保持
- journal の最初の 7 日はローカル + オフサイト二重化

## 5. restore 標準手順

1. 最新 Litestream replica または妥当な snapshot を選ぶ
2. SQLite を復元する
3. `journal_ingester_state` を開始点として journal replay する
4. `journal_ingester_state` が無い場合は `max(event_inbox.journal_offset)` を開始点にする
5. reconcile を実行する
6. projection rebuild を実行する
7. `PRAGMA integrity_check` を実行する
8. `replication_lag_seconds`, `snapshot_age_seconds`, `journal_offsite_lag_seconds` を計測する
9. health check を実行する
10. 成功判定を記録する

補足:
- `journal_ingester_state` と `event_inbox.journal_offset` がずれていても `unique(source, delivery_id)` により replay は冪等である

## 6. restore 後の確認

最低限確認すること:

- Router が起動できる
- reconcile が完走する
- `implementing` task の再開判定が期待どおりである
- projection rebuild が欠損補完として完走する
- `system_degraded` が不必要に残っていない

## 7. restore drill の記録

毎回残すべきもの:

- 実施日
- 実施者
- 復元元
- restore 成功 / 失敗
- replay, reconcile, rebuild, health check の結果
- 次回までの改善点

`deploy/restore-drill.sh` の exit 0 条件:

- restore 成功
- replay 成功
- reconcile 成功
- projection rebuild 成功
- `PRAGMA integrity_check = ok`
- `replication_lag_seconds <= 60`
- `snapshot_age_seconds <= 86400`
- `journal_offsite_lag_seconds <= 60`

## 8. Secret incident 復旧

主要受益ペルソナ: P2, P4

### 8.1 age 秘密鍵喪失

全管理者が age 秘密鍵を失うと `deploy/secrets/*.env` / `*.yml` は復号不能になる。全 secret を **再発行** する。

1. Discord Developer Portal で `TASK_RELAY_DISCORD_BOT_TOKEN` を Reset Token
2. Forgejo service account Settings または admin CLI で `TASK_RELAY_FORGEJO_TOKEN` を新規発行し、webhook secret も新値に更新
3. MinIO 管理画面で Litestream access/secret key を新規発行
4. 新 age キーペアを生成 (`docs/guides/secret-management.md §2` 参照)
5. `.sops.yaml` を新 age 公開鍵のみで再構成
6. 新 secret を `sops encrypt` で暗号化し直し commit
7. deploy 先 host で `git pull` → `./deploy/secrets-decrypt.sh --force` → `systemctl restart task-relay.target`
8. 影響時間: 30-60 分、通常運用停止を伴う
9. post-incident audit: journal / system_events から漏洩範囲を特定

age 秘密鍵の紛失リスクを減らすため、各管理者は以下を実施:
- 秘密鍵を 1Password / Bitwarden vault に backup
- 紙 backup + 施錠保管庫 (age キーは短い ASCII)
- 秘密鍵を GitHub / クラウドプレーン保管 **禁止**

### 8.2 Token compromise (漏洩検知)

Discord / Forgejo / MinIO いずれかの token が漏洩疑いを受けた場合:

1. **即時 revoke** (Developer Portal / Forgejo admin UI / MinIO) — 旧 token を失効
2. 新 token を発行 → sops edit → commit → deploy (runbook §9.3 emergency revoke 参照)
3. `system_events` / journal を時系列で洗い出し、漏洩期間中の task / projection / DM 送信履歴を audit
4. 影響範囲を本書 §9 incident log に記録
5. 必要なら関連 admin の age 秘密鍵も rotation (§8.1 手順)

### 8.3 incident log template

本書末尾に時系列で追記する:

```
## YYYY-MM-DD secret incident

- 発生時刻:
- 対象 secret:
- 原因:
- revoke 時刻:
- 新 secret 発行時刻:
- deploy 完了時刻:
- 影響時間 (service 停止):
- audit 結果 (漏洩期間中の task / DM / projection 件数):
- 再発防止策:
```

## 9. 非対象

- Redis 永続バックアップ
- Forgejo mirror からの state 復元
- 手作業による task truth 編集

## 10. 関連ドキュメント

- [runbook.md](runbook.md)
- [reconcile.md](reconcile.md)
- [failure-injection.md](failure-injection.md)
- [../guides/secret-management.md](../guides/secret-management.md)
