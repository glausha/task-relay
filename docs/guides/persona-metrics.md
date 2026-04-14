# task-relay Persona Metrics

出典:
- [personas.md](../../personas.md)
- [basic-design-v1.0.md](../../basic-design-v1.0.md)
- [detailed-design-v1.0.md](../../detailed-design-v1.0.md)

本書は persona-driven 設計が実運用で成立しているかを測るための計測指標集である。
新機能投入前のチェックリストではなく、運用中の feedback loop を回すための観測点と tripwire を定義する。

制約:
- P1 / P2 / P3 / P4 の通常指標は、システム生成または既存運用で機械的に取得できる記録に依存する。
- 反ペルソナ再評価だけは、人間起点の要求を扱うため、issue / PR / review の構造化シグナルと git 履歴を入力にしてよい。
- 手書きメモ、任意のふりかえり文書、任意監査メモは source of truth にしない。

## 1. 目的

- P1 / P2 / P3 / P4 の成功条件が運用で満たされているかを測る
- `反ペルソナ再評価` と `TBD 解除` を主観ではなく観測で決める
- 通知設計、ops-card、runbook の有効性を継続的に見直す

## 2. P1 指標

見たいこと:
- 通知過多になっていないか
- コスト / rate 悪化に気づける前提が崩れていないか

指標:
- 1 日あたり Discord 介入通知件数
- 深夜帯の通知件数
- `stop_new_tasks` 発動回数
- rate 枯渇回数
- 月次 LLM / ストレージ費

tripwire:
- 介入通知が 1 日平均 10 件を超えた週が 2 週続いたら通知設計を再評価する
- rate 枯渇または `stop_new_tasks` が月 3 回以上なら cost / rate 可視化を正式着手する
- LLM + ストレージ費が月額 10,000 円を超えたら cost / rate 可視化を正式着手する

## 3. P2 指標

見たいこと:
- 深夜に局所 / 全体を素早く判別できるか
- 過剰介入や誤介入が起きていないか

指標:
- `system_degraded` 発生時の初回応答までの時間
- 深夜帯の `/unlock` / `/retry-system` 実行回数
- `system_degraded` と breaker open の発生件数
- 深夜帯の停止系 task 件数 (`human_review_required`, `needs_fix`, `cancelled`)

tripwire:
- 深夜帯の `system_degraded` または breaker open が月 2 件以上なら `/status` 表示と用語正規化を見直す
- 深夜帯の停止系 task が増加傾向なら P2 向け ops-card を見直す

## 4. P3 指標

見たいこと:
- state 遷移、rebuild、再送、truth / mirror 境界を説明できるか

指標:
- projection rebuild 実行件数
- `--force` を伴う rebuild 件数
- projection 永久失敗 (`system_events`) 件数
- mirror 読み取り専用違反の通知件数

tripwire:
- `--force` rebuild が月 2 件以上なら rebuild 運用を見直す
- mirror 読み取り専用違反通知が発生したら運用表示と runbook を更新する

## 5. P4 指標

見たいこと:
- restore drill と保守手順が反復可能か

指標:
- `replication_lag_seconds > 60` の発生件数
- 最新 snapshot の鮮度違反 (`> 24h`) 件数
- journal sync lag `> 60s` の発生件数
- retention sweep が検出した orphan metadata / orphan file 件数

tripwire:
- replication lag または journal sync lag の閾値違反が継続するなら DR / retention 設計を見直す
- 最新 snapshot 鮮度違反が 1 回でも起きたら P4 向け運用手順を見直す
- orphan metadata / orphan file が 1 件でも検出されたら retention runbook を修正する

## 6. 反ペルソナ再評価の測定

入力源:
- `anti-persona:` prefix または専用 label を持つ issue / PR / review
- `personas.md`, `personas/`, `docs/guides/`, `docs/reference/runbook.md` に対する git 履歴
- `anti-persona` を理由に含む設計変更 PR

判定:
- 同種の要望や utterance が 3 件/月以上、2 か月連続で出たら `personas.md` §5.4 の tripwire を満たす

## 7. データソース

- `projection_outbox` の送信済み `discord_alert` 行
- `system_events`
- `tool_calls`
- `rate_windows`
- provider billing dashboard / invoice export

## 8. 関連ドキュメント

- [notification-guide.md](notification-guide.md)
- [../reference/runbook.md](../reference/runbook.md)
- [ops-cards/post-incident-audit.md](ops-cards/post-incident-audit.md)
