# task-relay Secret 管理セットアップマニュアル

本書は `basic-design-v1.0.md §9.3` の要求「sops + age で管理し、env 注入のみ」を実装するための実務手順。I1 (実機結合検証) 前に必須。

**対象 secret** (全て暗号化して管理):

| 変数                                 | 用途                    | 出所                     |
| ------------------------------------ | ----------------------- | ------------------------ |
| `TASK_RELAY_DISCORD_BOT_TOKEN`       | Discord bot 認証        | Discord Developer Portal |
| `TASK_RELAY_FORGEJO_TOKEN`           | Forgejo API runtime token | Forgejo service account Settings / CLI |
| `TASK_RELAY_FORGEJO_WEBHOOK_SECRET`  | Webhook HMAC 検証       | 32 バイト以上の乱数      |
| MinIO access/secret key (Litestream) | 別ホスト S3 互換 backup | MinIO 管理画面           |

**対象外** (secret ではない):
- `TASK_RELAY_ADMIN_USER_IDS` / `TASK_RELAY_DISCORD_GUILD_IDS` 等の数値 ID
- `TASK_RELAY_FORGEJO_BASE_URL` 等のエンドポイント URL

平文 `.env` 配布は **禁止**。全て sops 暗号化 → deploy 時復号 → systemd EnvironmentFile へ注入、の流れで運用する。

---

## 1. 前提ツール

### 1.1 age

```bash
# Ubuntu/Debian
sudo apt install age
# or: Homebrew / binary from https://github.com/FiloSottile/age/releases
```

### 1.2 sops

```bash
# https://github.com/getsops/sops/releases から最新版
curl -L https://github.com/getsops/sops/releases/latest/download/sops-v3.12.2.linux.amd64 -o /tmp/sops
sudo install -m 755 /tmp/sops /usr/local/bin/sops
sops --version  # 3.9 以上を確認
```

---

## 2. age キーペアの生成

### 2.1 プライマリ管理者 (P4) の鍵生成

```bash
# 運用ホストで生成 (まとめ役 1 名)
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt
chmod 600 ~/.config/sops/age/keys.txt

# 公開鍵を取得 (後で .sops.yaml の recipient に入れる)
grep "^# public key:" ~/.config/sops/age/keys.txt
# => 例: # public key: age1abc...xyz
```

### 2.2 補助管理者 (追加 P4 全員、on-call)

複数の管理者で secret を共有する場合は、各人が自分のホストで同様にキーを生成し、公開鍵を保管管理者へ提出する。公開鍵はそれぞれ異なるので `.sops.yaml` に列挙する。

### 2.3 秘密鍵のバックアップ (推奨)

秘密鍵を紛失すると復号不能になり secret 再発行が必要。以下いずれかを実施:

- **1Password / Bitwarden vault** に `keys.txt` の内容を secure note として保存
- **紙バックアップ** + 施錠保管庫 (age キーは短い ASCII なので OK)
- ⚠️ 秘密鍵を GitHub / クラウドプレーン保管は禁止

---

## 3. `.sops.yaml` 配置

repo root (`path/to/root/task-relay/`) に作成:

```yaml
# .sops.yaml
creation_rules:
  - path_regex: ^task-relay/deploy/secrets/.*\.env$
    age: |
      age1abc...xyz,  # 管理者 1 の公開鍵
      age1def...uvw   # 管理者 2 の公開鍵 (複数 admin の場合)
  - path_regex: ^task-relay/deploy/secrets/litestream\.yml$
    age: |
      age1abc...xyz,  # 管理者 1 の公開鍵
      age1def...uvw   # 管理者 2 の公開鍵 (複数 admin の場合)
```

複数 recipient を列挙することで、どの管理者の秘密鍵でも復号可能になる。

`git add .sops.yaml` → commit 可能 (公開鍵は公開情報)。

---

## 4. 初回 secret 暗号化

### 4.1 task-relay 用 env

```bash
cd task-relay

# 平文 template を作業用に作成 (commit しない)
cat > /tmp/task-relay.env <<'EOF'
TASK_RELAY_DISCORD_BOT_TOKEN=<Developer Portal の token>
TASK_RELAY_FORGEJO_TOKEN=<Forgejo service account token>
TASK_RELAY_FORGEJO_WEBHOOK_SECRET=<32+ バイト乱数、例: python -c "import secrets;print(secrets.token_hex(32))">
EOF

# sops で暗号化
mkdir -p deploy/secrets
sops --encrypt /tmp/task-relay.env > deploy/secrets/task-relay.env
# (.sops.yaml の path_regex にマッチしたので自動で age 暗号化)

# 平文を消す
shred -u /tmp/task-relay.env

# 暗号化ファイルは git 管理下に置ける
git add deploy/secrets/task-relay.env
```

### 4.2 Litestream config

```bash
cat > /tmp/litestream.yml <<'EOF'
dbs:
  - path: /var/lib/task-relay/state.sqlite
    replicas:
      - type: s3
        endpoint: https://minio.internal:9000
        bucket: task-relay-wal
        path: state
        access-key-id: <MinIO access key>
        secret-access-key: <MinIO secret key>
        sync-interval: 10s
EOF

sops --encrypt /tmp/litestream.yml > deploy/secrets/litestream.yml
shred -u /tmp/litestream.yml
git add deploy/secrets/litestream.yml
```

---

## 5. deploy/secrets-decrypt.sh (新設)

`install.sh` から呼ばれる復号スクリプト。systemctl start 前に実行する。

```bash
#!/usr/bin/env bash
# deploy/secrets-decrypt.sh
# sops 暗号化 secret を /etc/task-relay/ に復号配置
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/var/lib/task-relay}"
ETC_DIR="/etc/task-relay"

umask 077
mkdir -p "$ETC_DIR"
chown task-relay:task-relay "$ETC_DIR"

# task-relay.env
sops --decrypt "${REPO_ROOT}/deploy/secrets/task-relay.env" > "${ETC_DIR}/task-relay.env"
chmod 600 "${ETC_DIR}/task-relay.env"
chown task-relay:task-relay "${ETC_DIR}/task-relay.env"

# litestream.yml
sops --decrypt "${REPO_ROOT}/deploy/secrets/litestream.yml" > "${ETC_DIR}/litestream.yml"
chmod 600 "${ETC_DIR}/litestream.yml"
chown task-relay:task-relay "${ETC_DIR}/litestream.yml"

echo "secrets decrypted to ${ETC_DIR}/"
```

`chmod +x deploy/secrets-decrypt.sh`。

---

## 6. install.sh への連携

```bash
# deploy/install.sh の適切な位置に追加
"${REPO_ROOT}/deploy/secrets-decrypt.sh"
```

install.sh を実行する際、実行ユーザは sops + age キーにアクセスできる必要があるので管理者 (= age 秘密鍵を持つユーザ) が手動実行する。自動 CI からは回さない。

---

## 7. systemd unit の扱い

既存の `EnvironmentFile=/etc/task-relay/task-relay.env` はそのまま利用。復号は install.sh で済んでいる。ただし念のため:

```ini
# deploy/systemd/task-relay-*.service
[Service]
EnvironmentFile=/etc/task-relay/task-relay.env
# ↑ 600 permission で task-relay user のみ read
```

**alternative**: systemd-creds (`LoadCredential=`) を使えば systemd が起動時に automatic decrypt できるが、sops 指定の設計から外れるため採用しない (設計見直しが必要ならその時に再検討)。

---

## 8. Rotation 手順

設計書 §9.5 では rotation SLA が TBD だが、暫定 SLA を以下に設定する (運用で調整):

| Secret                 | 頻度   | トリガ                |
| ---------------------- | ------ | --------------------- |
| Discord bot token      | 90 日  | periodic / compromise |
| Forgejo token          | 90 日  | periodic / compromise |
| Forgejo webhook secret | 180 日 | periodic              |
| MinIO credentials      | 180 日 | periodic              |

### 8.1 Rotation 作業フロー (Discord bot token 例)

```bash
# 1. 新 token を Developer Portal で生成 (Reset Token) — 旧 token は即時失効
# 2. sops で暗号化 env を edit
cd task-relay
sops deploy/secrets/task-relay.env
# エディタ起動 → TASK_RELAY_DISCORD_BOT_TOKEN を新値に更新 → 保存 (自動再暗号化)
# 3. commit
git add deploy/secrets/task-relay.env
git commit -m "rotate: Discord bot token"
# 4. deploy host で pull + decrypt + systemd restart
git pull
./deploy/secrets-decrypt.sh
sudo systemctl restart task-relay-discord-bot.service task-relay-projection.service
# 5. 動作確認 (slash command 応答、DM 送信)
```

**Downtime**: Discord bot 再接続のみ、数秒。

### 8.2 Emergency revoke (compromise)

1. **即座** Developer Portal で **Reset Token**
2. 新 token で `sops edit` → commit → deploy
3. Discord Developer Portal の OAuth2 既存 grant を revoke (念のため)
4. `docs/reference/runbook.md` の incident log に記録

Forgejo token / MinIO credentials も同様。**先に revoke、後で rotate**。

---

## 9. 複数管理者での key 共有

新しい管理者が追加された場合:

```bash
# 1. 新管理者がローカルで age キー生成
age-keygen -o ~/.config/sops/age/keys.txt
grep "^# public key:" ~/.config/sops/age/keys.txt  # 新 age1... を取得

# 2. 既存管理者が .sops.yaml に新 recipient を追加
vim .sops.yaml  # age: age1old...,age1new...

# 3. 既存の暗号化ファイルを新 recipient で再暗号化
sops updatekeys deploy/secrets/task-relay.env
sops updatekeys deploy/secrets/litestream.yml

# 4. commit
git add .sops.yaml deploy/secrets/
git commit -m "access: add new admin age key"
```

削除時は `.sops.yaml` から該当 age 公開鍵を削除 → `updatekeys` を実行。さらに既存 secret の **値自体** も rotation しておくと安全 (既 value は元管理者が記録していた可能性があるため)。

---

## 10. Key 喪失時の復旧

age 秘密鍵を全管理者が失った場合、**暗号化 secret は復号不能**。復旧手順:

1. Developer Portal / Forgejo / MinIO で全 secret を **revoke** (漏洩していないが、安全第一)
2. 各サービスで新 secret を **再発行**
3. 新 age キーペアを生成
4. `.sops.yaml` を書き直し (新 age 公開鍵のみ)
5. 新 secret で暗号化 env を再作成 (section 4 参照)
6. deploy 更新

復旧時間は 30-60 分程度。通常運用停止を伴うので key バックアップ (section 2.3) が critical。

---

## 11. Restore drill との連携

`deploy/restore-drill.sh` は **Step 0 で secrets-decrypt.sh を自動実行 (実装済)**。root 権限かつ暗号化ファイル (`deploy/secrets/task-relay.env` と `litestream.yml`) が実在する場合のみ decrypt → `/etc/task-relay/` に配置する。test/dev 環境 (非 root or 暗号化ファイル不在) では silent skip するので既存 pytest を壊さない。

これにより別ホストで restore しても secret が正しく配置され、`systemctl start task-relay.target` が env 欠落で失敗することが無い。

### restore-drill.sh 実装済フロー

```
Step 0: secrets-decrypt  (root + deploy/secrets/*.env|*.yml 実在時のみ、force 上書き)
Step 1: db-check
Step 2: journal-replay
Step 3: reconcile
Step 4: health-check
Step 5: success criteria (max_lag_seconds 判定)
```

---

## 12. セキュリティ境界

- 平文 secret はメモリ / `/etc/task-relay/task-relay.env` / `/etc/task-relay/litestream.yml` (いずれも 600 perms) にのみ存在
- git には **暗号化版のみ** commit、`.gitignore` に `/tmp/*.env` や平文派生物を除外
- `detailed-design §12.1` redact allowlist により、log/stderr/trace に secret が漏洩しないよう既に実装済
- Discord / Forgejo webhook 通信は https 前提 (TLS は nginx reverse proxy で終端)

---

## 13. 代替案 (採用しないが記録)

| 方式                  | 長所                         | 短所                     | 採用? |
| --------------------- | ---------------------------- | ------------------------ | ----- |
| **sops + age** (推奨) | 設計指定、軽量、git-friendly | age キー配布が手動       | ✅     |
| systemd-creds         | 自動 decrypt、TPM2 対応      | systemd-only、設計変更要 | ❌     |
| HashiCorp Vault       | エンタープライズ機能完備     | 単一ホスト運用には重厚   | ❌     |
| 1Password CLI         | チーム共有が楽               | 外部サービス依存         | ❌     |
| AWS Secrets Manager   | マネージド                   | クラウド依存、課金       | ❌     |

設計書 v1.0 は **sops + age** を指定しているため本書もそれに従う。v1.1 以降で方式変更する場合は basic-design §9.3 改訂 + migration 手順策定。

---

## 14. チェックリスト (I1 前)

- [ ] age + sops インストール済
- [ ] age キーペア生成済、公開鍵取得済
- [ ] age 秘密鍵を安全にバックアップ済
- [ ] `.sops.yaml` を repo に配置済
- [ ] `deploy/secrets/task-relay.env` を sops 暗号化済
- [ ] `deploy/secrets/litestream.yml` を sops 暗号化済
- [ ] `deploy/secrets-decrypt.sh` を新設済 (chmod +x)
- [ ] `deploy/install.sh` から decrypt スクリプト呼び出し済
- [ ] `/etc/task-relay/task-relay.env` の permission 600 確認
- [ ] `systemctl start task-relay.target` で全サービス起動成功
- [ ] Discord bot online / Forgejo webhook 受理 / Litestream 動作確認
- [ ] `docs/reference/runbook.md` に rotation 手順を追記
- [ ] `docs/reference/disaster-recovery.md` に secret 復旧手順を追記

---

## 15. 付録: 参考資料

- [sops 公式](https://getsops.io/docs/)
- [age 公式](https://github.com/FiloSottile/age)
- [basic-design-v1.0.md §9.3 Secret 管理](../../basic-design-v1.0.md)
- [detailed-design-v1.0.md §12.1 Secret redact allowlist](../../detailed-design-v1.0.md)
- [OWASP Secrets Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html)

---

## 実装状況メモ

- `deploy/secrets-decrypt.sh`, `deploy/install.sh`, `deploy/restore-drill.sh` の secret 復号連携は実装済み
- `docs/reference/runbook.md` と `docs/reference/disaster-recovery.md` の secret 運用手順は記載済み
- 残る設計上の open item は `basic-design §9.5 TBD` の rotation SLA を v1.1 で確定すること
