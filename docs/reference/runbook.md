# task-relay Runbook

出典:
- [personas.md](../../personas.md)
- [basic-design-v1.0.md](../../basic-design-v1.0.md)
- [detailed-design-v1.0.md](../../detailed-design-v1.0.md)

本書は P2 と P4 が実際に参照する運用手順の正本である。
詳細アルゴリズムや DB 制約ではなく、状況判断と安全な介入順序を固定する。

## 1. 基本姿勢

- まず局所障害か全体障害かを判別する。
- task state は Router が変える。手元で直接 DB を触らない。
- 深夜対応では「状況悪化を止める」を優先し、調査の完璧さを求めすぎない。
- `/unlock` と `/retry-system` は admin 操作であり、最初の一手にしない。

### 1.1 operator scope の正規化

本節を operator 向け scope ラベルの source of truth とする。
`docs/reference/state-machine.md` は state machine 自体の参照ビューであり、operator scope は本節に従う。

| State / 事象 | scope ラベル | operator 向け意味 |
|---|---|---|
| `new` | 進行中 | 受理直後。まだ危険シグナルではない |
| `planning` | 進行中 | plan 作成中 |
| `plan_pending_approval` | 人間待ち | 自然停止中。承認待ちであり障害ではない |
| `plan_approved` | 進行中 | dispatch 待ち |
| `implementing` | 進行中 | 実装中 |
| `implementing_resume_pending` | 局所保留 | 再開条件確認待ち。深夜に無理に再開しない |
| `needs_fix` | 局所障害 | task 単位で再実装や再計画が必要 |
| `reviewing` | 進行中 | review 中 |
| `human_review_required` | 局所障害 | task 固有の停止。個別 task を精査する |
| `cancelled` | 局所停止 | 人間が明示停止した task |
| `done` | 完了 | 完了済み |
| `system_degraded` | 全体障害 | system-wide 異常。全体復旧可否を先に判断する |
| breaker open | 全体保護中 | 全体保護のため新規 dispatch を止めている。task state ではない |

運用ルール:
- `全体` には `全体障害` と `全体保護中` の 2 種がある。
- breaker open は `system_degraded` と同じ文言で扱わない。
- `plan_pending_approval` は人間待ちだが障害ではない。
- P2 の 1 画面判定は Discord `/status` の scope ラベルを主とする。

## 2. インターフェースの使い分け

### 2.1 Discord

向いている操作:
- 状態確認
- `approve`
- `retry`
- `cancel`
- `critical`
- `retry-system`
- `/status`

向いていない操作:
- 長い履歴調査
- 詳細ログ確認
- restore drill
- 大きな保守作業

### 2.2 PC 側

向いている操作:
- Forgejo での履歴確認
- `runner-cli reconcile-report --last`
- `runner-cli projection-rebuild --task <task_id>`
- restore drill
- 監査と長時間調査

## 3. 最初の判定

### 3.1 `system_degraded`

意味:
- system-wide 異常
- 新規 dispatch 停止

先に確認すること:
1. 影響が system 全体か
2. 既存 in-flight は継続中か
3. いま即介入が必要か、待機可能か

### 3.2 `human_review_required`

意味:
- task 単位停止
- 人間判断待ち

先に確認すること:
1. task 単位の問題か
2. subprocess が既に止まっているか
3. 朝の自分へ引き継げる状態か

### 3.3 `needs_fix`

意味:
- 実装結果は出たが、そのまま先へ進めない

先に確認すること:
1. 範囲外変更か
2. 一般エラーか
3. 同じ `plan_rev` で再実装できるか

## 4. よく使う操作

### 4.1 承認

使う場面:
- `plan_pending_approval`
- `human_review_required` かつ review 済みで manual gate を閉じたい

期待結果:
- `plan_pending_approval -> plan_approved`
- または `human_review_required -> done`

使ってはいけない場面:
- 停止理由を読まずに流すとき
- 全体障害中に個別 task だけ閉じたいとき

### 4.2 再試行

使う場面:
- `needs_fix`
- `human_review_required`

選び方:
- 同じ plan を継続するなら `/retry`
- plan からやり直すなら `/retry --replan`

期待結果:
- `/retry` は通常 `implementing` へ戻る
- `/retry --replan` は `planning` に戻る

### 4.3 キャンセル

使う場面:
- task をこれ以上進めたくない
- AI 暴走懸念でまず止めたい

期待結果:
- Router が `cancelled` に遷移
- その後、ToolRunner 親が停止通知または polling で状態変化を検知し、subprocess へ SIGTERM、必要なら SIGKILL

確認ポイント:
- task が `cancelled` になっている
- subprocess が継続していない

### 4.4 `/unlock <branch>`

使う場面:
- branch lease が stuck して待機列が前進しない

前提:
- admin only
- 最初の一手ではない

期待結果:
- compare-and-delete で Redis lease を解放
- wait queue の先頭 task を `queued` に戻し dispatch を再評価
- `branch_tokens.last_token` は巻き戻さない

注意:
- 生きている subprocess は次回 renew / assert で失敗し停止する

### 4.5 `/retry-system --stage <stage>`

使う場面:
- breaker open や system-wide 異常が解消済みで、dispatch を再開したい

前提:
- admin only
- health check を伴う

期待結果:
- breaker 集計は global に reset
- health check 成功後 `internal.system_recovered` を journal に append

注意:
- stage 引数は health check と dispatch resume 対象の指定であり、個別 task の再試行ではない

## 5. 深夜障害対応

### 5.1 目標

- 1 分以内に「寝てよい / 今動く」を決める
- 状況悪化を止める
- 翌朝の自分が再構成できる状態を残す

### 5.2 判断順序

1. `system_degraded` か task 局所停止かを判定する
2. いま必要なのが全体復旧か task 停止維持かを決める
3. 即介入不要なら、朝に持ち越す
4. 即介入が必要な場合だけ admin 操作に進む

### 5.3 やってはいけないこと

- `human_review_required` を全体障害と誤認する
- 状況整理前に `/unlock` や `/retry-system` を連打する
- 深夜に複数 task をまとめて処理しようとする

### 5.4 ペルソナ衝突パターン集

1. 深夜に `human_review_required` が来たときは、P2 を優先する。
   判断: まず安全停止と朝への引き継ぎを優先し、P3 的な深掘り監査は翌朝の [../guides/ops-cards/post-incident-audit.md](../guides/ops-cards/post-incident-audit.md) に回す。
2. restore drill 実施中に通常 task が投入されたときは、計画保守 window 中は P4 を優先する。
   判断: 保守境界を崩して task をねじ込まない。投入は待機または別 window に回す。
3. projection rebuild 中に `system_degraded` や breaker open が出たときは、P2 を優先する。
   判断: まず全体復旧可否を判断し、P3 的な rebuild 正当性監査は復旧後に回す。
4. 通勤中に `needs_fix` を見つけたときは、P1 を優先する。
   判断: モバイルでは「今止めるか、PC まで待つか」だけを決め、差分や因果の精査は PC に寄せる。
5. queue stuck が疑われ `/unlock` したくなったが原因未調査のときは、深夜は P2、日中監査枠では P3 を優先する。
   判断: 深夜は悪化防止のため最小介入で復旧し、原因追跡は後続 audit で行う。

## 6. restore drill / 保守

### 6.1 目標

- runbook どおりに保守を反復実行できる
- task 実行系と保守系を混ぜない
- 作業後に安全に運用継続できるか確認する

### 6.2 restore drill の標準順序

1. 最新 replica / snapshot を確認する
2. SQLite を復元する
3. `journal_ingester_state` を開始点に journal replay する
4. reconcile を実行する
5. projection rebuild を実行する
6. health check を実行する
7. 成功判定を記録する

詳細は [disaster-recovery.md](disaster-recovery.md) を参照する。

## 7. 再構築系コマンド

### 7.1 `runner-cli projection-rebuild --task <task_id>`

用途:
- DB 欠損後の outbox 補完

規則:
- 既送信 remote mirror の強制再生成を目的にしない
- デフォルトは `ON CONFLICT DO NOTHING` で欠損だけを補う
- `--force` は未送信 outbox の再生成だけに使う

### 7.2 `runner-cli reconcile-report --last`

用途:
- 直近起動時 reconcile の結果確認

見るべき項目:
- `implementing -> plan_approved`
- `implementing -> implementing_resume_pending`
- `implementing -> human_review_required`
- wait queue 清掃件数

## 8. 成功判定

- 深夜対応で局所障害と全体障害を誤読しない
- `cancel` 後に subprocess が継続しない
- restore drill を四半期ごとに再実行できる
- runbook を見れば、前回メモがなくても同じ判断と手順を再現できる

## 9. 関連ドキュメント

- [state-machine.md](state-machine.md)
- [reconcile.md](reconcile.md)
- [disaster-recovery.md](disaster-recovery.md)
- [failure-injection.md](failure-injection.md)
- [../guides/ops-cards/system-degraded.md](../guides/ops-cards/system-degraded.md)
- [../guides/ops-cards/human-review-required.md](../guides/ops-cards/human-review-required.md)
- [../guides/ops-cards/post-incident-audit.md](../guides/ops-cards/post-incident-audit.md)
- [../guides/ops-cards/restore-drill.md](../guides/ops-cards/restore-drill.md)
