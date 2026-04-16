# task-relay 基本設計 v1.0

Webhook / Gateway 駆動の AI agent タスクランナー。個人開発・単一ホスト運用を前提とする。
本書は v1.5 を「基本設計」と「詳細設計」に分割した上位文書である。アーキテクチャ・責務境界・契約・非機能要件・状態モデルの骨格のみを扱い、具体的な列定義・閾値・実装手順は `detailed-design-v1.0.md` に委譲する。

---

## 0. 決定事項サマリ

### 0.1 ペルソナ・トレーサビリティ

本書の設計判断は [personas.md](personas.md) を上位方針とする。
設計判断を含む `##` / `###` 節には `主要受益ペルソナ` を必須で明記し、レビュー時に「この決定は誰のどの不安や成功条件を守るか」を逆引きできるようにする。
例外は `用語集`, `関連ドキュメント`, 純粋な file path / enum / command 一覧のような説明補助節だけとする。

ペルソナ裁定の前提:

- 常に P1 を最優先する。
- 障害対応中は P2 を secondary 最優先とする。
- 保守作業中は P4 を secondary 次優先とする。
- 安全性と運用性が同等なら P3 の説明可能性を優先する。

主要対応表:

| 設計テーマ | 主要受益ペルソナ | 守るもの |
|---|---|---|
| Discord 受理、モバイル確認、通知抑制 | P1, P2 | スマホでの短時間判断、深夜の即時判定 |
| Cancel / stop 伝播、breaker、system_degraded | P2 | 状況悪化防止、局所障害と全体障害の判別 |
| truth / mirror 分離、outbox、idempotency、監査履歴 | P3 | 後追い説明可能性、因果追跡 |
| backup / restore / retention / restore drill | P4 | 反復可能な保守、壊してはいけない境界の明確化 |
| branch lease、manual gate、resume | P1, P2, P3 | 安全な自動化、再開判断、説明可能な停止理由 |

| 項目 | 決定 |
|---|---|
| エージェント構成 | 3 分離 (Planning: Claude Opus / Execution: Claude Code / Review: Codex GPT-5) |
| 業務状態の真実源 | SQLite WAL primary |
| 受理前耐久化 | `ingress_journal` への append+fsync を ACK 条件とする |
| SQLite 保全 | Litestream による継続 WAL レプリケーション + 日次 snapshot |
| 目標 RPO / RTO | RPO 60 秒以内、RTO 30 分以内 |
| Redis の役割 | branch lease, rate cache, duplicate cache。真実を保持しない |
| Forgejo の役割 | UI / Git / Webhook source。mirror であり真実源ではない |
| Router 単独責務 | `tasks.state` / `state_rev` / critical / manual_gate / resume_target_state の変更は Router のみ |
| 状態遷移経路 | 外部イベントも internal 制御イベントも `journal -> inbox -> Router` を通す |
| ToolRunner 停止経路 | Router commit 後の state-change 通知を親プロセスが受け、subprocess を停止する |
| Projection 冪等性 | 決定的 `idempotency_key` を付与し、remote dedup 可能な識別子を全 stream が持つ |
| Breaker open の意味 | 新規 dispatch 停止のみ。既存 in-flight task は継続し、task state は直ちに変えない |
| 通知方針 | `needs_fix` / `human_review_required` / `system_degraded` / breaker open 時のみ Discord DM |

---

## 1. 設計原則

主要受益ペルソナ: P1, P2, P3, P4

### 1.1 不変条件

主要受益ペルソナ: P1, P2, P3, P4

1. 業務状態の真実は SQLite のみとする。
2. 外部入力は ACK 前に `ingress_journal` へ durable append されていなければならない。
3. Router が処理するイベントは必ず SQLite の inbox に存在する。
4. すべての状態遷移は単一 SQLite transaction で完結し、`state_rev` を 1 増やす。
5. Redis にしか存在しない重要状態は持たない。Redis は落ちても再構築できる。
6. branch 排他は lock ではなく lease である。lease は `lease_branch` ごとの dispatch lane を守るために使い、lease 喪失後の作業継続は禁止する。
7. 外部反映は outbox パターンで行い、業務 transaction に外部 API を混ぜない。
8. Forgejo mirror は観測対象であり、状態判定の真実源には使わない。
9. `tasks.state` / `state_rev` / task truth flags (`critical`, `manual_gate_required`, `resume_target_state`) は Router だけが更新する。
10. lease 喪失など内部要因による状態遷移も inbox を経由する。

### 1.2 受容する制約

主要受益ペルソナ: P1, P2, P4

- HA クラスタは組まない。primary は 1 台。
- primary 破損時のイベント消失は許容しないため、journal + continuous replication を必須化する。
- Discord interaction は 3 秒制約を守るが、受理成功は journal durable append 完了を条件とする。

---

## 2. アーキテクチャ

主要受益ペルソナ: P1, P2, P3, P4

```
Forgejo Webhook ----┐
Discord Gateway ----┼-> ① Ingress
runner-cli ---------┘      |
                            v
                    ② Durable Ingress Journal
                            |
                            v
              ③ Journal Ingester -> SQLite Inbox + Router
                                     |       |        |
                                     v       v        v
                                 Planning Execution Review
                                           |
                                           v
                         ④ Projection / Retention / DLQ / Observability
                                           |
                                     Forgejo / Discord
```

### 2.1 データフロー

主要受益ペルソナ: P1, P3

1. Ingress が入力を canonical event 化する。
2. event を `ingress_journal` に append+fsync する。
3. journal ingester が SQLite inbox に反映する。
4. Router が inbox を消費し状態遷移と outbox 生成を行う。
5. projection worker が Forgejo / Discord に反映する。
6. retention worker が log / metadata の retention を実行する。

### 2.2 ストアと役割

主要受益ペルソナ: P3, P4

| ストア | 役割 | 耐久性 |
|---|---|---|
| SQLite WAL | 業務状態の真実源 | Litestream で継続レプリケーション |
| ingress_journal | 受理前イベントの耐久バッファ | append-only、fsync 必須 |
| Redis | lease / rate cache / duplicate cache | SQLite から再構築可能 |
| Forgejo | UI / Git / Webhook source | mirror |

---

## 3. コンポーネント責務境界

主要受益ペルソナ: P1, P2, P3, P4

### 3.1 コンポーネント一覧

主要受益ペルソナ: P1, P2, P3, P4

| コンポーネント | 責務 |
|---|---|
| Ingress (Forgejo webhook / Discord gateway / runner-cli) | 署名検証、canonical event 化、journal への durable append |
| Journal Ingester | journal → inbox への idempotent な反映 |
| Router | inbox 消費、状態遷移、outbox 生成、truth flag 更新 |
| ToolRunner (親プロセス) | subprocess 起動、lease renew、log 収集、`feature_branch` / `worktree_path` / `last_known_head_commit` などの operational metadata 更新、internal event 発火 |
| ToolRunner (subprocess) | Planner / Executor / Reviewer 実行。Redis は read-only assert のみ |
| Projection Worker | outbox から Forgejo / Discord への反映、cursor 更新 |
| Retention Worker | log / journal / metadata の retention |
| Reconcile Worker | 起動時の整合回復。直接 state は変えず internal event を発火 |

### 3.2 書き込み権限マトリクス

主要受益ペルソナ: P3, P4

| コンポーネント | tasks.state/state_rev/flags | plans | inbox | outbox | branch_waiters / branch_tokens | journal_ingester_state | operational metadata | rate_windows | system_events | journal append | external |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Ingress | - | - | - | - | - | - | - | - | - | ○ | - |
| Journal Ingester | - | - | ○ (insert) | - | - | ○ | - | - | - | - | - |
| Router | ○ | ○ | ○ (processed_at) | ○ (insert) | ○ | - | - | - | ○ | - | - |
| ToolRunner 親 | - | - | - | - | - | - | ○ (`tool_calls`, `last_known_head_commit`) | ○ (header 観測時) | ○ | ○ (internal events) | - |
| ToolRunner sub | - | - | - | - | - | - | - | - | - | - | - |
| Projection Worker | - | - | - | ○ (sent_at / attempt) | - | - | ○ (`projection_cursors`) | ○ (rate 観測時) | ○ | - | ○ |
| Retention Worker | - | - | - | - | - | - | ○ (`tool_calls` の null 化) | - | - | - | - |
| Reconcile Worker | - | - | - | - | - | - | - | - | ○ | ○ (internal events) | - |

- 「operational metadata」は task truth に影響しないカラムのみを指す。
- `system_events` は運用監視用の append-only 記録であり、複数コンポーネントが書き込む。

### 3.3 IPC と停止伝播

主要受益ペルソナ: P1, P2

- Router は `cancelled` / `system_degraded` / `human_review_required` などの停止系 state を commit した後、best-effort で ToolRunner 親へ state-change 通知を送る。
- ToolRunner 親は通知経路に加え polling fallback を持ち、自 task の停止系 state を観測したら subprocess に SIGTERM を送る。
- subprocess が一定時間内に終了しない場合は SIGKILL する。
- この停止通知は task truth ではなく operational control であり、state 更新そのものは Router のみが行う。
- breaker open は停止系 state-change 通知の対象ではない。breaker open は新規 dispatch を止めるだけで既存 in-flight task は継続する。

### 3.4 並行性モデル

主要受益ペルソナ: P1, P2, P3

| コンポーネント | 並列度 | 根拠 |
|---|---|---|
| Router | 1 | SQLite single-writer。状態遷移の全順序を担保 |
| Journal Ingester | 1 | `journal_ingester_state` は 1 行。順序保証 |
| Projection Worker | `(task_id, stream, target)` ごとに 1 in-flight | FIFO を守るため |
| ToolRunner | task ごとに 1 親プロセス。並列 task 数の上限は運用設定 | `lease_branch` ごとの publication lane を branch lease で直列化 |
| Retention Worker | 1 | idempotent だが重複実行の意味なし |

---

## 4. 用語集

| 用語 | 意味 |
|---|---|
| `task_id` | task の一意 ID。Router が `new` 受理時に採番 |
| `state_rev` | task 状態遷移の単調増加版 (transaction ごと +1) |
| `plan_rev` | 同一 task 内の plan 版。再計画ごと +1 |
| `event_id` | inbox における event の一意 ID |
| `delivery_id` | 外部送信側の重複検知用 ID (Forgejo/Discord 固有) |
| `outbox_id` | projection outbox 行の ID (単調増加) |
| `idempotency_key` | projection 送信の決定的キー。remote dedup に使用 |
| `request_id` | Discord ingress handler が append 前に採番する追跡 ID |
| `call_id` | ToolRunner subprocess 1 回の実行を識別 |
| `fencing_token` | branch 排他のための世代トークン。逆行しない |
| `lease_branch` | wait queue / lease / `/unlock` の対象となる integration lane。通常は task が最終的に載せたい base branch |
| `feature_branch` | task 専用 worktree で Git mutate する branch。remote push と人間 PR の head になる |
| `worktree_path` | task 専用 worktree の絶対 path。resume 判定と cleanup の対象 |
| `journal_offset` | ingress_journal 内の byte offset。ingester の再開点 |
| `resume_target_state` | `system_degraded` 遷移時に退避した復帰先 state |
| `stream` | projection の配信路種別 (`task_snapshot` / `task_comment` / `task_label_sync` / `discord_alert`) |
| `target` | projection の送信先識別子 (例: Forgejo issue URL、Discord user ID) |

---

## 5. 外部契約

主要受益ペルソナ: P1, P2, P3, P4

### 5.1 Forgejo Webhook

主要受益ペルソナ: P1, P3

受理する event allowlist:

| event | 用途 | 受理 |
|---|---|---|
| `issues` | task 作成、close/reopen、label 操作 | する |
| `issue_comment` | `/approve` 相当の人間指示 | する |
| `pull_request` | 状態遷移には使わない | 無視 |
| `push`, `release`, `wiki` 等 | 非対象 | 無視 |

- `reviewing -> done` 判定に `pull_request` Webhook は使わない。完了判定は Reviewer 出力と manual gate のみ。
- Pull Request は Forgejo 上の mirror artifact であり、task truth や完了判定の source of truth にしない。
- 手動入力を受理する label は `critical` / `human_review_required` / `cancelled` の allowlist のみ。
- label は SQLite 真実から再計算し、`task_label_sync` stream が Forgejo labels を単独管理する。

### 5.2 Discord Gateway

主要受益ペルソナ: P1, P2

- Gateway event loop をブロックしない。ingress handler は writer thread 経由で journal に append。
- ACK 前 durable を維持しつつ heartbeat を詰まらせないため、append の完了を短時間だけ待って応答する。
- 失敗応答には `request_id` のみを含める。`task_id` は ingest 後でないと存在性確認できないため含めない。
- client 側自動リトライは禁止。再送は人間が明示的に行う。

### 5.3 runner-cli

主要受益ペルソナ: P1, P2, P4

- すべての管理操作は `cli` source の event として journal → inbox を通す。直接 DB を書き換えない。

### 5.4 Agent adapter 契約

主要受益ペルソナ: P1, P3

Planner / Executor / Reviewer は adapter interface を介して呼ばれる。adapter は以下の能力記述を持つ:

- `supports_request_id: bool` — provider が client 採番 request_id を受け付けるか。timeout 自動再試行の可否に影響する。

入出力契約:

| adapter | 入力 | 出力 |
|---|---|---|
| Planner | task goal, repo context | plan 構造 (goal / sub_tasks / allowed_files / acceptance_criteria 等) |
| Executor | 承認済み plan, `allowed_files ∪ auto_allowed_patterns` | `changed_files`, exit status, failure_code |
| Reviewer | plan, diff, test/log 参照 | criterion ごとの status, overall decision, policy_breaches |

契約 schema の version 管理:

- 各 adapter 出力には `contract_version` を付与する。Planner は `planner_version`、Reviewer は `reviewer_version`、Executor は adapter version として扱う。
- schema 破壊的変更は新 version を採番し、同一 task 内で version を跨ぐ変更は行わない。

### 5.5 Projection stream 契約

主要受益ペルソナ: P3

| stream | 対象 | 目的 | 順序保証 |
|---|---|---|---|
| `task_snapshot` | Forgejo issue body / frontmatter | 現在 state と plan_rev 等の mirror | 新しい `state_rev` が古い snapshot を supersede。同一 `state_rev` 内の差分は payload-sensitive identity で識別 |
| `task_comment` | Forgejo issue comment | 監査コメント | FIFO 厳守 |
| `task_label_sync` | Forgejo issue labels | allowlist label の desired set 同期 | FIFO 厳守 |
| `discord_alert` | Discord DM | 人間介入要請 | FIFO 厳守 |

- 全 stream の `idempotency_key` は決定的生成とし、rebuild で同じ key を再現できなければならない。
- `task_snapshot` は同一 `state_rev` の rebuild / 補正でも payload 差分を識別できなければならない。したがって snapshot の identity は `state_rev` 単独ではなく payload-sensitive である。
- rebuild の conflict policy は「既送信 mirror を壊さず欠損のみを補完する」ことを基本契約とする。具体アルゴリズムは詳細設計に委譲する。

---

## 6. 状態モデル

主要受益ペルソナ: P2, P3

### 6.1 状態一覧

主要受益ペルソナ: P2, P3

```
new
planning
plan_pending_approval
plan_approved
implementing
implementing_resume_pending
needs_fix
reviewing
human_review_required
done
cancelled
system_degraded
```

### 6.2 マクロ遷移図

主要受益ペルソナ: P2, P3

```
new -> planning -> plan_pending_approval -> plan_approved -> implementing -> reviewing -> done
           |              |                        ^              |              |
           |              +-- /retry --replan -----+              |              +-- fail --> needs_fix --> implementing
           |              +-- /cancel --> cancelled                |              +-- human_review_required
           |                                                       +-- 範囲外 / 一般エラー --> needs_fix
           |                                                       +-- lease_lost --> human_review_required
           |                                                       +-- infra_fatal --> system_degraded
           |                                                       +-- 再起動検知 --> implementing_resume_pending
           +-- validator 超過 --> human_review_required

plan_approved -- /critical on --> plan_pending_approval
* -> system_degraded (infra_fatal)
system_degraded -> resume_target_state (guard 再評価)
* -> cancelled (/cancel, done 除く)
```

具体的な Trigger / Guard の表、internal event の列挙、`/critical` / `/approve` / `/retry` / `/unlock` の詳細処理は詳細設計§6 を参照する。

### 6.3 状態遷移の原則

主要受益ペルソナ: P2, P3

- 状態遷移は Router が SQLite transaction 内で行う。
- self-loop や flag 変更のみの遷移でも `state_rev` は 1 増やす。
- `* -> system_degraded` では現在 state を `resume_target_state` に退避する。復帰時に guard を再評価し、成立しない場合は `human_review_required` にフォールバックする。
- `resume_target_state=implementing` の復帰では lease と subprocess の連続性を仮定してはならない。通常の dispatch をやり直せる state に戻して再開する。
- `resume_target_state=reviewing` の復帰では、以前の reviewer 実行結果を再利用せず review を再実行する。
- `planning` / `reviewing` の timeout が自動再試行不可 (`supports_request_id=false` または retry budget 消費済み) の場合、ToolRunner 親は `internal.planner_timeout` / `internal.reviewer_timeout` を journal に append し、Router は `human_review_required` に送る。
- 停止の原因切り分けを以下の原則で行う:
  - **task 局所停止** (`human_review_required` / `needs_fix`) は task 固有の異常に限る。lease 喪失、境界検査失敗、executor 内ロジックエラー、validator 超過などを含む。
  - **system-wide degrade** (`system_degraded`) は infrastructure 原因に限る。SQLite / Redis 障害、adapter FATAL の連鎖などを含む。
- breaker open は task state 遷移ではなく dispatch 制御である。既存 in-flight task を `system_degraded` に送ってはならない。
- `done` / `cancelled` は projection 永久失敗のみを理由に `system_degraded` へ遷移させない。
- `lease_branch` 直列化は manual merge の安全性を保証するものではない。守る対象は「同一 integration lane に対する relay-managed feature branch の publish 順序」と「reviewer / 人間が観測する HEAD 一貫性」である。

operator 向け scope への写像:

| 内部概念 | operator 向け scope | 取り扱い |
|---|---|---|
| `human_review_required`, `needs_fix` | 局所障害 | task 単位で見る |
| `system_degraded` | 全体障害 | 全体復旧を先に判断する |
| breaker open | 全体影響 | 全体保護による dispatch pause。`system_degraded` と混同しない |

---

## 7. 故障モデル

主要受益ペルソナ: P2, P3

### 7.1 failure class

主要受益ペルソナ: P2, P3

| class | 方針 |
|---|---|
| TRANSIENT | 自動再試行する |
| UNKNOWN | 限定的に再試行する。Execution stage の副作用ありケースでは再試行しない |
| FATAL | 再試行しない。breaker 集計対象 |

具体的な `failure_code` enum とマッピング、repair retry 回数、Circuit Breaker の閾値は詳細設計§10 を参照する。

### 7.2 idempotency

主要受益ペルソナ: P3

- Planner / Reviewer adapter は `supports_request_id=true` の場合、同一 `request_id` を再試行で継続利用する。
- Execution stage は workspace と Git mutation を含むため `timeout` / `oom_killed` の自動再試行を禁止する。
- `supports_request_id=false` の adapter では `planning` / `reviewing` の `timeout` 自動再試行を禁止する。

### 7.3 Circuit Breaker 方針

主要受益ペルソナ: P2

- breaker 集計は `failure_code` 単位の global 集計。
- open 中は新規 dispatch を停止する。既存 in-flight は kill しない。
- breaker open 自体は task を `system_degraded` に遷移させない。
- reset は admin only の手動操作を基本とし、時間経過による自動 half-open は導入しない。
- reset は `internal.system_recovered` event を経由して Router が判断する。

### 7.4 SQLite 破損・満杯時

主要受益ペルソナ: P2, P4

- 新規受理を停止し `system_degraded` を通知する。
- 実行中 subprocess には SIGTERM を送り、時限で SIGKILL する。
- 復旧後に journal replay から再開する。

### 7.5 Cancel / Stop 方針

主要受益ペルソナ: P1, P2

- `/cancel` は Router が `cancelled` へ遷移させた後、ToolRunner 親へ停止通知を送る。
- ToolRunner 親は停止通知または polling fallback により自 task の `cancelled` を観測し、subprocess を SIGTERM、必要に応じて SIGKILL する。
- `cancelled` 遷移後に subprocess が作業継続してはならない。

---

## 8. データモデル抽象

主要受益ペルソナ: P3, P4

### 8.1 論理モデル

主要受益ペルソナ: P3, P4

- `tasks`: task truth と task 単位 metadata。truth として `state`, `state_rev`, `critical`, `manual_gate_required`, `resume_target_state`, `lease_branch` を持ち、task metadata として `feature_branch`, `worktree_path`, `last_known_head_commit` を持つ。
- `plans`: task の plan 系列 (plan_rev ごと)。
- `event_inbox`: ingester が書き込む未処理 event キュー。Router の消費対象。
- `projection_outbox`: stream ごとの mirror 送信キュー。決定的 `idempotency_key` を持つ。
- `projection_cursors`: `(task_id, stream, target)` ごとに送信済み最新 state_rev を保持。
- `branch_waiters` / `branch_tokens`: `lease_branch` 排他 (wait queue と fencing token)。
- `journal_ingester_state`: ingester の再開位置。
- `tool_calls`: subprocess 実行のメタデータ。log は外部ファイル。
- `rate_windows`: provider rate window 状態。
- `system_events`: 運用監視用の event 記録。`task_id` で関連付け。

### 8.2 外部ファイル

主要受益ペルソナ: P3, P4

- `ingress_journal`: append-only NDJSON.zst。日単位 rotate。
- tool log: task / stage / call 単位で 1 ファイル。SQLite には path / hash / size のみ。

具体的な列定義と path 規則は詳細設計§4 を参照する。

---

## 9. セキュリティモデル

主要受益ペルソナ: P1, P2, P4

### 9.1 認証

主要受益ペルソナ: P1, P2, P4

| surface | 方式 |
|---|---|
| Forgejo Webhook | HMAC 必須。localhost でも省略しない |
| Discord slash command | Discord 接続 + 管理コマンドは `admin_user_ids` allowlist |
| runner-cli | ローカル Unix user 制御 |

### 9.2 認可

主要受益ペルソナ: P1, P2, P4

- `/approve` / `/critical` / `/retry` / `/cancel` は `task.requested_by` または `admin_user_ids`。
- `/unlock` / `/retry-system` は `admin_user_ids` のみ。
- `task.requested_by` は principal 形式の文字列で表現する。形式は ingress source ごとに次のとおり:
  - Forgejo Webhook 経由: `forgejo:<sender_login>`
  - Discord 経由: `discord:<user_id>`
  - runner-cli 経由: `cli:<unix_user>`
  - internal: 新規 task 採番には使われない
- `/approve` / `/critical` / `/retry` / `/cancel` の認可判定は、actor の同一 source 形式と
  `task.requested_by` の文字列一致で行う。`admin_user_ids` (Discord user_id 整数の allowlist) は
  source 横断で許可する。

`/unlock` の機能契約は§5 Ingress / runner-cli の手動介入手段として扱い、lease 実装規則は詳細設計で定義する。基本原則は「fencing token を巻き戻さず、stuck lease から wait queue を前進させる」ことに限る。`/unlock` 後、branch wait queue は即時に再評価され、次の dispatch は通常の lease 取得手順で行う。

### 9.3 Secret 管理

主要受益ペルソナ: P2, P4

- sops + age で管理し、env 注入のみ。worktree への配置を禁止する。
- redact allowlist は log / stderr / trace / system comment など「観測用出力」に適用する。`event_inbox.payload_json` / `projection_outbox.payload_json` は真実源として保持する。

### 9.4 情報境界

主要受益ペルソナ: P1, P3, P4

- Discord に出してよい情報: `task_id`, state, 件数, Forgejo URL。
- Discord に出してはならない情報: plan 本文、diff、log 詳細、secret、cost 明細。

### 9.5 Threat model (TBD)

主要受益ペルソナ: P1, P2, P3, P4

本書では以下の threat boundary を後続版で詰める:

- Discord token 漏洩時の被害範囲
- Forgejo / Redis が compromise された場合の task-relay への影響
- secret rotation の頻度と手順

---

## 10. 非機能要件

主要受益ペルソナ: P2, P4

### 10.1 耐久性 / 可用性

主要受益ペルソナ: P2, P4

- RPO 60 秒以内、RTO 30 分以内。
- Litestream は別ホスト MinIO bucket へ 10 秒以下間隔で継続 replicate する。
- オフサイト二重化は MinIO bucket replication により S3 互換 bucket へ 7 日保持する。
- journal は別ディスクに日単位 rotate で 30 日保持し、直近 7 日はローカル + オフサイト二重化する。
- SQLite 復元 + journal replay で「ACK 済みなのに永久喪失」を起こさない。

### 10.2 バックアップ対象

主要受益ペルソナ: P4

- SQLite primary / Litestream replica bucket / 日次 SQLite snapshot / ingress_journal / secret file。

### 10.3 DR drill

主要受益ペルソナ: P4

- 四半期に 1 回以上の restore drill を実施する。
- 成功判定は `replication_lag_seconds <= 60`, `snapshot_age_seconds <= 86400`, `journal_offsite_lag_seconds <= 60`, `PRAGMA integrity_check = ok`, reconcile 成功, projection rebuild 成功を満たすこと。

### 10.4 未確定項目 (TBD)

主要受益ペルソナ: P1, P2, P4

以下は次版で定量化する:

- 負荷想定 (task/day, event/sec, 並行 task 上限)
- 容量計画 (journal 30 日保持の実サイズ、SQLite 成長率)
- 可用性目標 (月次許容ダウンタイム)
- レイテンシ目標 (Discord ack p95, task 投入 → plan_approved)
- コスト上限 (LLM 月額、ストレージ)
- P1 向けコスト / rate 通知の threshold、集計 window、通知頻度、通知チャネル

### 10.5 Observability 方針

主要受益ペルソナ: P2, P3, P4

確定事項:

- 必須 SLI: ingest ack rate, router lag, projection lag, lease failure count, breaker state, rate window remaining。
- log は structured。`task_id` を correlation id として貫通させる。
- alert 経路は Discord DM。

未確定 (TBD):

- 各 SLI の SLO 数値目標。
- P1 向け cost / rate 通知は v1.0 の正本範囲外とする。v1.0 で確定するのは内部保護としての `rate window remaining` 観測と `stop_new_tasks` 判定まで。
- Discord DM 不達時の fallback 経路の要否。

### 10.6 Data lifecycle (TBD)

主要受益ペルソナ: P3, P4

- log / journal / metadata の retention は詳細設計で定義済み。
- `tasks` / `plans` / `outbox` の archive / purge 方針は次版で決定する。

---

## 11. モバイル運用

主要受益ペルソナ: P1, P2

- Discord は task 投入と簡易確認のみ。詳細は Tailscale 越しの Forgejo Web で扱う。
- P2 の「1 画面で判別する」surface は Discord `/status` を主とし、`局所障害 / 全体障害 / 全体保護中` を明示ラベルで返す。
- 情報境界は§9.4 に従う。

---

## 12. 実装ブロック

主要受益ペルソナ: P1, P2, P3, P4

詳細設計章番号は `detailed-design-v1.0.md` を参照する。

| ブロック | 概要 | 主な詳細設計章 |
|---|---|---|
| A | journal writer / replay / ingester | §1 書き込み順序, §3 Ingress, §11 Cold Start |
| B | SQLite schema / inbox router / state machine | §2 データモデル, §4 Router |
| C | Redis branch lease / fencing token / renew task | §5 Branch Lease |
| D | Planner / Executor / Reviewer adapters | §6 Agent adapter 契約 |
| E | compressed log writer | §7 ToolRunner とログ保持 |
| F | projection streams / ordering worker / rebuild / idempotency markers | §9 Projection 実装 |
| G | rate window estimation | §10 レート制御 |
| H | reconcile / resume logic / report | §11 Cold Start / Reconcile |
| I | Discord auth / command handling / failure UX | §3 Ingress, §12 認証・権限 |
| J | litestream / backup / restore drill | §13 Backup / DR |
| K | log retention worker | §7 ToolRunner とログ保持 |
| L | journal retention worker | §7 ToolRunner とログ保持 |

---

## 13. 関連ドキュメント

- `detailed-design-v1.0.md` (本書と対になる詳細設計)
- `docs/reference/schema.md`
- `docs/reference/state-machine.md`
- `docs/reference/runbook.md`
- `docs/reference/disaster-recovery.md`
- `docs/reference/reconcile.md`
- `docs/reference/failure-injection.md`
- `.versions.yaml`
