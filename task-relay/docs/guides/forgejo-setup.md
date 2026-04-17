# Forgejo サーバーセットアップマニュアル

task-relay の Forgejo Webhook ingress + Forgejo mirror projection を動かすために必要な準備手順。所要時間 15-30 分。

## 1. 前提

- Forgejo サーバーを動かすホストを用意している
- task-relay を動かすホストから `TASK_RELAY_FORGEJO_BASE_URL` へ outbound 接続できる
- Forgejo サーバーから task-relay の Webhook endpoint へ outbound 接続できる
- secret は sops + age で管理する。平文 `.env` の配布と commit はしない

**接続要件**:

| 接続元            | 接続先                                                             | 用途                                        |
| ----------------- | ------------------------------------------------------------------ | ------------------------------------------- |
| task-relay ホスト | `TASK_RELAY_FORGEJO_BASE_URL` (`https://forgejo.example.com` など) | issue body / comment / labels の projection |
| Forgejo サーバー  | `http://<task-relay-host>:8787/webhook/forgejo`                    | `issues` / `issue_comment` Webhook          |

同一ホスト運用なら `http://127.0.0.1:3000` と `http://127.0.0.1:8787` で閉じられる。別ホスト運用では 8 章のネットワーク経路を先に決める。

---

## 2. Forgejo サーバーの用意

**選択肢**:

a) **新規に Forgejo を立てる** (既定: task-relay 用に分離したい場合)
b) **既存 Forgejo を使う** (既存運用を流用したい場合)

### 2a. 新規セットアップ (Docker Compose)

リポジトリには最小構成の compose を [deploy/forgejo-compose.yml](/home/akala/Documents/Glauca/task-relay/task-relay/deploy/forgejo-compose.yml) として同梱している。`deploy/forgejo-compose.env.example` は **ローカルホストで Forgejo を立てる場合の例** になっている。先に環境ファイルを用意する。

```bash
sudo mkdir -p /etc/task-relay
sudo cp deploy/forgejo-compose.env.example /etc/task-relay/forgejo-compose.env
sudoedit /etc/task-relay/forgejo-compose.env
```

最低限、次を設定する。

- `FORGEJO_IMAGE`: 固定した Forgejo image tag
- `FORGEJO_ROOT_URL`: ブラウザで開く公開 URL
- 必要なら `FORGEJO_HTTP_PORT`, `FORGEJO_SSH_PORT`, `FORGEJO_UID`, `FORGEJO_GID`

同一ホストで動かすだけなら example の `FORGEJO_ROOT_URL=http://127.0.0.1:3000/` と port 値をそのまま使ってよい。

起動:

```bash
docker compose --env-file /etc/task-relay/forgejo-compose.env -f deploy/forgejo-compose.yml up -d
```

初回は `FORGEJO_ROOT_URL` で指定した URL を開き、Web UI の初期設定を完了する。compose は Docker named volume `forgejo-data` に `/data` を永続化するので、host 側 bind mount path を別途決めなくても開始できる。

初期設定画面の `Administrator account settings` では、最初の **admin user** を作成する。ここで作る user は後続の repository 作成、Webhook 登録、service account 作成に使う。`TASK_RELAY_FORGEJO_TOKEN` 用の runtime token はこの admin user ではなく、後で §2.3 / §5 に従って別途作る **service account** から発行する。

⚠️ `task-relay.target` は Forgejo を起動しない。Forgejo は `TASK_RELAY_FORGEJO_BASE_URL` から到達できる外部前提であり、compose 管理でも既存サーバーでもよい。`task-relay.target` を起動する前に、上の compose か既存運用の方法で Forgejo を先に利用可能にしておく。

### 2b. 既存 Forgejo を使う場合

何もしなくてよい。次だけ確認する。

- repository 作成権限がある
- 対象 repository で Webhook を追加できる
- token を発行できるユーザーがいる
- task-relay が接続する URL が `TASK_RELAY_FORGEJO_BASE_URL` として確定している

### 2.3. admin user + service account の作成

初回セットアップ時に **2 つのユーザー**を用意する:

| ユーザー                              | 用途                                                           | 必要権限                                        |
| ------------------------------------- | -------------------------------------------------------------- | ----------------------------------------------- |
| **admin user** (例: `relay-admin`)    | repo 作成 / webhook 登録 / 後続 service account 作成           | site admin                                      |
| **service account** (例: `relay-bot`) | runtime の token 所有者。`forgejo_sink` から API call する主体 | 対象 repo の **collaborator (write 権限)** のみ |

`admin user` は初回セットアップ画面の `Administrator account settings` で作成する。

既存サーバーを使う場合は admin user は既存のものでよい。service account は new user として別途作成し、対象 repository の Settings → Collaborators に **Write** 権限で追加する。本書 §3 以降の **repo 作成 / webhook 登録は admin user**、**§5 の runtime token 発行は service account** で行う。

service account の作成方法:

- UI: admin user で login し、Site Administration の user 管理画面から `relay-bot` などの通常ユーザーを追加する
- CLI: Forgejo を local Docker で動かしている場合は次でもよい。Compose で指定した `FORGEJO_UID` / `FORGEJO_GID` と同じ値で実行する

```bash
docker exec --user 1000:1000 forgejo forgejo admin user create \
  --username relay-bot \
  --password '<strong-password>' \
  --email relay-bot@example.local \
  --must-change-password=false
```

local example では `FORGEJO_UID=1000`, `FORGEJO_GID=1000` を前提にしている。`/etc/task-relay/forgejo-compose.env` で別の値にした場合は、`docker exec --user <uid>:<gid>` の値も同じに合わせる。

この service account には `--admin` を付けない。作成後、対象 repository の Settings → Collaborators に **Write** 権限で追加する。

---

## 3. Repository 作成

task-relay が mirror 先として使う repository を 1 つ用意する。Forgejo は truth source ではなく mirror なので、owner / repo 名は task-relay 側の設定と一致していればよい。

| 項目  | 例          | 対応する環境変数           |
| ----- | ----------- | -------------------------- |
| owner | `relay-bot` | `TASK_RELAY_FORGEJO_OWNER` |
| repo  | `relay`     | `TASK_RELAY_FORGEJO_REPO`  |

推奨:

- visibility は **private**
- task-relay 専用 repository に分離する
- owner は個人より organization / team のほうが保守しやすい

作成後、owner 名と repo 名を控える。

---

## 4. 必須ラベル作成

⚠️ **本章の §4.2 curl 例は token を要する**。線形に読むなら **先に §5 (Service Account Token 発行) を完了させ，本章に戻ってくる**。UI のみで作成する §4.1 だけなら token 不要。

task-relay の `MANAGED_LABELS` は `critical`, `human_review_required`, `cancelled` の 3 つで固定されている。設計書 `basic-design-v1.0.md §5.1` の手動入力 label allowlist も同じ 3 つである。projection は SQLite の truth からこの集合を再計算して Forgejo に push するので、repository 側に事前作成されていないと失敗する。

| label 名                | 用途                     | 推奨色    |
| ----------------------- | ------------------------ | --------- |
| `critical`              | critical フラグの mirror | `#B60205` |
| `human_review_required` | 人手確認待ちの mirror    | `#FBCA04` |
| `cancelled`             | cancelled 状態の mirror  | `#6E7781` |

### 4.1. UI で作成

1. 対象 repository を開く
2. 上部の **Issues** タブを開き、右上付近の **Labels** を押して label 管理画面に入る
3. **New Label** を 3 回実行し、上の表の label 名をそのまま作る
4. 色は任意でよい。推奨色を使うなら上の値を入れる

### 4.2. 欠落時の症状

1 つでも欠けていると projection が label ID を解決できず、`forgejo labels not found: ...` で失敗する。典型例は `forgejo labels not found: critical` である。

---

## 5. Service Account Token 発行

task-relay の Forgejo projection は `Authorization: token <TOKEN>` で `/api/v1/repos/{owner}/{repo}/...` にアクセスする。token は `TASK_RELAY_FORGEJO_TOKEN` に入れる。

### 5.1. UI で発行

task-relay の Forgejo projection は §2.3 の **service account** で発行した token を使う。admin user の token は使わない (権限過剰であり、本書の前提外)。

1. **service account でログイン** (admin から logout してから service account でログイン)
2. 右上のアバター → **Settings**
3. **Applications**
4. **Generate Token**
5. Token 名を `task-relay-runtime` などにする
6. **Specific repositories** を選び、対象 repo を 1 つだけチェック (Forgejo v15+ で利用可能、未対応バージョンでは All repositories)
7. scope は最小権限原則に従い次を付ける
   - **必須**: `write:issue` (issue body PATCH, comment POST, label PUT、`labels/*` API 全般を含む)
   - **任意**: `read:repository` (repo 存在確認 / debug 用、無くても projection は動く)

   過去版で `write:repository` を推奨していたが、`write:repository` は repo 設定変更権限を含む広範な scope であり projection には不要。Forgejo `token-scope` docs も最小権限を強く推奨している。
8. 生成された token をコピーし、安全な場所に保管する

⚠️ admin user の token を `TASK_RELAY_FORGEJO_TOKEN` に入れない。runtime 漏洩時に admin 権限まで奪われる。
⚠️ token は secret なので、そのままチャットや issue に貼らない。

### 5.2. CLI 代替

Forgejo ホストで CLI が使えるなら次でもよい。

`--username` には **service account** (例: `relay-bot`) を指定する (admin user ではない)。

```bash
forgejo admin user generate-access-token --username <service-account> --token-name task-relay-runtime --scopes write:issue --raw
```

出力された token を `TASK_RELAY_FORGEJO_TOKEN` に格納する。


---

## 6. Webhook Secret の生成

Webhook の HMAC-SHA256 検証に使う secret を 1 つ生成する。task-relay は `X-Forgejo-Signature` を検証し、値は `TASK_RELAY_FORGEJO_WEBHOOK_SECRET` に入れる。

```bash
python -c "import secrets;print(secrets.token_hex(32))"
```

32 バイトの乱数が 16 進文字列で出る。生成した値を控え、Forgejo の Webhook 設定と task-relay の環境変数に同じ値を入れる。

---

## 7. Webhook 登録

task-relay の Webhook endpoint は `POST /webhook/forgejo` で、既定の listen は `127.0.0.1:8787` である。

### 7.1. Repo Webhook を追加

1. 対象 repository を開く
2. **Settings** → **Webhooks**
3. **Add Webhook** → **Forgejo**
4. 次を設定する

| 項目                 | 値                                               |
| -------------------- | ------------------------------------------------ |
| Target URL           | `http://<task-relay-host>:8787/webhook/forgejo`  |
| HTTP Method          | `POST`                                           |
| POST Content Type    | `application/json`                               |
| Secret               | 6 章で生成した値                                 |
| Authorization Header | 空のまま (task-relay は HMAC のみ使うので不要)   |
| Branch filter        | 空のまま (issues / issue_comment では機能しない) |
| Active               | ON                                               |

### 7.2. Trigger On

Forgejo の version により event 選択 UI が異なる。`Custom Events` がある UI ではそれを選び、ない UI では event checkbox 群から次だけ ON にする。

- `Issues`
- `Issue Comments`

次は OFF にする。

- `Pull Requests`
- `Push`
- `Releases`
- `Wiki`
- その他未使用イベント

`Custom Events` という見出しが見えなくても問題ない。重要なのは repository webhook が `issues` と `issue_comment` を task-relay に送ることだけである。

`pull_request` を OFF にするのは設計書 `basic-design-v1.0.md §5.1` に合わせるためである。task-relay が受理する event allowlist は次のとおり。

| event                                     | 受理条件                                        | 備考                                    |
| ----------------------------------------- | ----------------------------------------------- | --------------------------------------- |
| `issues`                                  | `opened`, `closed`, `reopened`, `label_updated` | task 作成 / close / reopen / label 操作 |
| `issue_comment`                           | `created` かつ slash command 一致               | comment edit は無視                     |
| `pull_request`, `push`, `release`, `wiki` | 受理しない                                      | mirror artifact 扱い                    |

受理する slash command は次の 7 個のみ。

- `/approve`
- `/retry`
- `/cancel`
- `/critical on`
- `/critical off`
- `/unlock`
- `/retry-system`

⚠️ `/unlock` と `/retry-system` は **Forgejo comment 経由では事実上受理されない**。これらは `admin_user_ids` (Discord user_id 整数の allowlist) によって認可されるが、Forgejo comment の sender は `forgejo:<sender_login>` 形式となり Discord user_id とマッチしないため、router の admin guard で reject される。これら 2 つの管理 command は **Discord slash command または `runner-cli` 経由のみ**で運用する。同様に `/retry --replan` 形式は CLI のみで Forgejo comment では未対応。

---

## 8. ネットワーク経路

既定では task-relay の Forgejo Webhook ingress は `127.0.0.1:8787` で待ち受ける。systemd では `deploy/systemd/task-relay-forgejo-webhook.service` が `task-relay ingress-forgejo --serve` を起動し、bind 先は `TASK_RELAY_FORGEJO_WEBHOOK_HOST` / `TASK_RELAY_FORGEJO_WEBHOOK_PORT` で上書きできる。

Forgejo が別ホストにある場合は、次のどちらかにする。

### 8.1. reverse proxy で公開する (推奨)

nginx などで TLS 終端し、backend を localhost に流す。

```nginx
server {
    listen 443 ssl;
    server_name relay.example.com;

    location /webhook/forgejo {
        proxy_pass http://127.0.0.1:8787/webhook/forgejo;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

この場合、Forgejo の Target URL は `https://relay.example.com/webhook/forgejo` にする。

### 8.2. `0.0.0.0` で待ち受ける

やむを得ない場合だけ `TASK_RELAY_FORGEJO_WEBHOOK_HOST=0.0.0.0` を使う。この設定は推奨しない。少なくとも firewall 制限と Tailscale などの private network を併用する。

HMAC が一次防衛なので TLS は必須ではないが、別ホスト運用では TLS を推奨する。

---

## 9. .env への設定

この章は **task-relay 実行環境を自前で持つ前提** である。Forgejo がセルフホストか SaaS かは関係なく、設定先は Forgejo サーバーではなく **task-relay を動かす環境** になる。systemd 運用なら `/etc/task-relay/task-relay.env`、開発運用なら `~/.config/task-relay/task-relay.env`、container / PaaS ならその基盤の secret / env injection を使う。**task-relay 自体を自分で動かさない構成ではこの章は使えない。**

`basic-design-v1.0.md §9.3` により worktree (`task-relay/.env`) への配置は禁止されているため、開発時も以下のいずれかを使う。worktree 直下に `.env` を置かない。

- `~/.config/task-relay/task-relay.env` (700 perm dir 内、user 個別)
- `/etc/task-relay/task-relay.env` (本番と同じ場所、root + chmod 600)

どちらも `direnv` や `systemd-run --setenv-file` 等で読み込ませる。

```ini
# Forgejo
TASK_RELAY_FORGEJO_BASE_URL=https://forgejo.example.com
TASK_RELAY_FORGEJO_OWNER=<owner>
TASK_RELAY_FORGEJO_REPO=<repo>
TASK_RELAY_FORGEJO_TOKEN=<step 5 の token>
TASK_RELAY_FORGEJO_WEBHOOK_SECRET=<step 6 の secret>
TASK_RELAY_FORGEJO_WEBHOOK_HOST=127.0.0.1
TASK_RELAY_FORGEJO_WEBHOOK_PORT=8787
```

補足:

- `TASK_RELAY_FORGEJO_BASE_URL` の default は `http://localhost:3000`
- `TASK_RELAY_FORGEJO_WEBHOOK_HOST` の default は `127.0.0.1`
- `TASK_RELAY_FORGEJO_WEBHOOK_PORT` の default は `8787`
- secret 分類: `TASK_RELAY_FORGEJO_TOKEN` と `TASK_RELAY_FORGEJO_WEBHOOK_SECRET` の 2 つだけが sops 暗号化対象。`OWNER` / `REPO` / `BASE_URL` / `HOST` / `PORT` は secret ではない
- `task-relay` は worktree `.env` を自動読込しない。開発時は `direnv` / `systemd-run --setenv-file` / `set -a; . ~/.config/task-relay/task-relay.env; set +a` などで明示的に注入する

⚠️ secret を含むので平文のまま commit しない。sops 暗号化は `secret-management.md` を参照する。
⚠️ `TASK_RELAY_FORGEJO_WEBHOOK_SECRET` を空文字のまま起動すると `verify_signature` が空 secret 用の HMAC を受理してしまう。必ず §6 で生成した値を入れる。
⚠️ `/etc/task-relay/task-relay.env` は `chown task-relay:task-relay && chmod 600` で task-relay user のみ read 可能にする (詳細は `secret-management.md §5`)。

---

## 10. 動作確認

受理だけを見るなら 10.1-10.2 で足りる。end-to-end の mirror 更新まで確認するなら 10.3-10.4 まで実施する。

### 10.1. listener 起動

開発環境では 1 つ目の terminal で Webhook ingress を起動する。

```bash
cd task-relay
uv run task-relay ingress-forgejo --serve
```

end-to-end 確認では別 terminal で次も起動しておく。

```bash
uv run task-relay ingester
uv run task-relay router
uv run task-relay projection
```

本番では上の代わりに `task-relay-forgejo-webhook.service`, `task-relay-journal-ingester.service`, `task-relay-router.service`, `task-relay-projection.service` を使う。`task-relay-forgejo-webhook.service` は `Requires=task-relay-router.service` で router 起動が前提条件 (運用ミスでの ingress 単独起動を防ぐ意図)。本番起動時は `task-relay.target` 一括起動を推奨する。

### 10.2. Issue 作成で `issues.opened` を確認

対象 repository で新しい issue を 1 件作る。まず `journalctl -u task-relay-journal-ingester -f` (本番) または ingester プロセスの stdout (開発) を見て新しい event ID が log に流れることを確認すれば疎通は成立する。`event_type` の確証が欲しい場合のみ以下の Python snippet を補助手段として使う。

```bash
python - <<'PY'
from pathlib import Path
import io
import zstandard

path = sorted(Path("var/task-relay/journal").glob("*.ndjson.zst"))[-1]
with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(path.read_bytes())) as reader:
    lines = reader.read().decode("utf-8").splitlines()
for line in reversed(lines):
    if '"event_type":"issues.opened"' in line:
        print(line)
        break
PY
```

### 10.3. Issue comment の `/approve` を確認

同じ issue に `/approve` とだけコメントする。task-relay は `issue_comment` の `created` を受理し、body が一致すれば canonical event `/approve` に変換する。確認は §10.2 と同様に `journalctl -u task-relay-journal-ingester -f` または ingester stdout で行い、必要なら以下の snippet で `event_type` を裏取りする。

```bash
python - <<'PY'
from pathlib import Path
import io
import zstandard

path = sorted(Path("var/task-relay/journal").glob("*.ndjson.zst"))[-1]
with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(path.read_bytes())) as reader:
    lines = reader.read().decode("utf-8").splitlines()
for line in reversed(lines):
    if '"event_type":"/approve"' in line:
        print(line)
        break
PY
```

### 10.4. projection で issue body / labels 更新を確認

`uv run task-relay projection` が動いていれば、router が outbox を作成した後に Forgejo issue の body と labels が更新される。次を確認する。

- issue body が task-relay の mirror 内容に更新される
- `critical`, `human_review_required`, `cancelled` の managed labels が SQLite truth に合わせて付け替わる
- repository 側で手動に追加した managed 外 label は残る
- 逆に **managed allowlist 内 (`critical` / `human_review_required` / `cancelled`) で SQLite truth に存在しない label は projection が削除する**。Forgejo 側で手動に `critical` を付けても、SQLite truth が critical=false なら次回 sync で外れる。critical を入れたい場合は `/critical on` slash command で truth を更新する

issue body は **YAML frontmatter + markdown 本文** で書き換えられる。形式は `---\n<key>: <value>\n---\n\n<body>` で、frontmatter 部分は task-relay が再生成のたび上書きする。手動で frontmatter 内のキー値を書き換えても次回 projection で上書きされるため、**手動編集は frontmatter 外の本文部分のみ**にする。

managed labels の同期は SQLite から再計算して push するので、Forgejo 側だけ直しても最終的には task-relay の truth に戻る。

---

## 11. トラブルシューティング

| 症状                                       | 原因                                                                                                                            | 対処                                                                                        |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `401 invalid signature`                    | secret 不一致、または `X-Forgejo-Signature` 欠損                                                                                | Forgejo の Secret と `TASK_RELAY_FORGEJO_WEBHOOK_SECRET` を同じ値にし、Webhook を保存し直す |
| `200 ignored`                              | event が allowlist 外、または action 不一致                                                                                     | Trigger On を `Issues` と `Issue Comments` のみにし、Issue Comment は新規作成コメントで試す |
| `forgejo labels not found: critical`       | 4 章の label が未作成                                                                                                           | `critical`, `human_review_required`, `cancelled` を repository に作成する                   |
| `PATCH /issues` で `401` / `403`           | token scope 不足、または token が無効                                                                                           | §5 に従い `write:issue` を含む新 token を発行して `TASK_RELAY_FORGEJO_TOKEN` を更新する     |
| Webhook が届かない                         | firewall、port 不一致、Webhook Active=OFF                                                                                       | Target URL、listen host/port、Active、reverse proxy、firewall を確認する                    |
| `500 internal error` / `json decode error` | Webhook の **POST Content Type** が `application/x-www-form-urlencoded` で body が `payload=...` 形式となり `json.loads` が失敗 | §7.1 表に従い `application/json` を選び直す                                                 |
| rate limit が出る                          | Forgejo 側の per-user limit が厳しい                                                                                            | Forgejo admin 設定で rate limit を確認し、task-relay 専用 user に分離する                   |

---

## 12. チェックリスト (I1 前)

- [ ] Forgejo サーバーが起動し、UI に login できる
- [ ] 対象 owner / repo を作成し、`TASK_RELAY_FORGEJO_OWNER` / `TASK_RELAY_FORGEJO_REPO` を控えた
- [ ] repository visibility を private にした
- [ ] `critical`, `human_review_required`, `cancelled` の 3 label を作成した
- [ ] `TASK_RELAY_FORGEJO_TOKEN` 用 token を発行し、安全に保管した
- [ ] `TASK_RELAY_FORGEJO_WEBHOOK_SECRET` を生成し、安全に保管した
- [ ] Webhook を `POST /webhook/forgejo` に登録した
- [ ] Trigger On を `Issues` と `Issue Comments` のみにした
- [ ] `pull_request`, `push`, `release`, `wiki` を OFF にした
- [ ] `.env` に `TASK_RELAY_FORGEJO_*` を設定した
- [ ] secret を平文 commit していない
- [ ] 実 issue 作成で `202 Accepted` が返ることを確認した
- [ ] Issue 作成で `issues.opened` が journal に入ることを確認した
- [ ] `/approve` comment が canonical event として通ることを確認した
- [ ] projection で issue body / labels が更新されることを確認した
- [ ] `TASK_RELAY_FORGEJO_WEBHOOK_SECRET` が空文字でない (空のまま起動すると HMAC 検証が事実上無効化される)
- [ ] `systemctl is-active task-relay-forgejo-webhook.service` が `active`
- [ ] `systemctl is-active task-relay-journal-ingester.service` が `active`
- [ ] `systemctl is-active task-relay-router.service` が `active`
- [ ] `systemctl is-active task-relay-projection.service` が `active`

---

## 13. セキュリティ注意

- `TASK_RELAY_FORGEJO_TOKEN` または `TASK_RELAY_FORGEJO_WEBHOOK_SECRET` が漏洩したら、**先に revoke、後で rotate** の順で対処する。具体手順は `secret-management.md §8.2` を参照する
- token は個人 admin ではなく専用 service account で発行する。権限範囲と監査の切り分けがしやすく、runtime 漏洩時の影響も限定できる
- `basic-design-v1.0.md §9.3` の secret 管理 / redact allowlist と、`§9.4` の情報境界に従い、Forgejo mirror には secret、詳細 log、作業中の内部メモを載せない。Forgejo は truth source ではない mirror として扱う
- Webhook listener は既定の `127.0.0.1` のまま使うのが安全である。外部公開する場合も HMAC は必須で、省略しない

### 13.1. Webhook secret rotate 手順 (Forgejo 固有)

Forgejo webhook の Secret フィールドは **single-secret** で新旧 overlap を持てない。切替の瞬間に届く delivery は必ず一方で `401 invalid signature` を返す。periodic rotate と compromise (漏洩) では手順が異なる。

#### periodic rotate (計画的、低トラフィック窓を確保できる場合)

1. 新 secret 生成: `python -c "import secrets;print(secrets.token_hex(32))"`
2. Forgejo の対象 webhook で **Active=OFF** にして delivery を一時停止
3. `sops` で `deploy/secrets/task-relay.env` を編集し新値を保存
4. `sudo deploy/secrets-decrypt.sh` を実行して `/etc/task-relay/task-relay.env` を更新
5. `sudo systemctl restart task-relay-forgejo-webhook.service`
6. Forgejo の Webhook Secret 欄を新値に差し替え保存
7. **Active=ON** に戻す
8. 実 issue 作成で webhook 到達を確認

OFF→ON 間に発生したはずの issue / comment は Forgejo に残るので、ON 後の手動 `/approve` 等で必要なら再投入する。

#### compromise rotate (即時、burst を許容)

1. Forgejo webhook で即座に **Active=OFF** (漏洩 secret での攻撃 delivery を止める)
2. 旧 secret を Forgejo webhook 設定から消す
3. 新 secret 生成 → `sops` 編集 → decrypt → restart (上の 1-5 と同じ)
4. Forgejo Webhook Secret に新値を入れて保存
5. Active=ON、実 issue 作成で webhook 到達を確認
6. 監視で `401 invalid signature` の burst が止まったことを確認

両ケースとも `secret-management.md §8.2` の「先に revoke、後で rotate」原則 (compromise は revoke = Forgejo 側 secret を空にする) と整合。


### 13.2. Token rotate 手順 (Forgejo 固有)

1. Forgejo UI または CLI (§5) で **新 token を発行** (旧 token はまだ有効のまま)
2. `sops` で env 更新 → `sudo deploy/secrets-decrypt.sh` → `sudo systemctl restart task-relay-projection.service task-relay-router.service`
3. **新 token で動作確認** (issue body PATCH 等が成功)
4. **旧 token を Forgejo UI から revoke**

新 token を先に配備してから旧 token を revoke するため通常は停止を最小化できるが、Forgejo 公式 docs に zero transient failure の保証は無い。compromise 時は overlap を捨てて先に旧 token を revoke する。

---

## 14. 付録: 参考資料

- [Forgejo Documentation](https://forgejo.org/docs/)
- [Forgejo Webhooks](https://forgejo.org/docs/latest/user/webhooks/)
- [Forgejo Access Token Scope](https://forgejo.org/docs/latest/user/token-scope/)
- [Forgejo CLI](https://forgejo.org/docs/latest/admin/command-line/)
- [Forgejo Installation with Docker](https://forgejo.org/docs/latest/admin/installation-docker/)
- [Forgejo Installation from Binary](https://forgejo.org/docs/next/admin/installation/binary/)
- [basic-design-v1.0.md §5.1](../../basic-design-v1.0.md)
- [secret-management.md](./secret-management.md)
- [discord-setup.md](./discord-setup.md)
