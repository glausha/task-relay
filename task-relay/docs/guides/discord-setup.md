# Discord サーバー / Bot セットアップマニュアル

task-relay の Discord Gateway (ingress) + Discord DM sink (projection) を動かすために必要な準備手順。所要時間 10-15 分。

## 前提

- Discord アカウントを持っている
- ブラウザで Discord Developer Portal にアクセスできる
- task-relay を運用するホストから `discord.com:443` に outbound 接続できる

---

## 1. Discord サーバー (Guild) の用意

**選択肢**:

a) **既存の個人サーバー or チーム用サーバー**をそのまま使う (推奨: 検証初期)
b) **新規サーバーを作成** (推奨: 運用分離する場合)

### 1a. 既存サーバーを使う場合

何もしなくてよい。そのサーバーに bot を招待する。ただし:
- サーバーの **管理権限 (Manage Server)** を持っていること
- task-relay 関係者以外に bot slash command が見えるのが許容範囲か確認

### 1b. 新規サーバー作成

1. Discord アプリ / Web 左サイドバー → **`+`** ボタン
2. **Create My Own** → **For me and my friends** を選択
3. Server Name: `task-relay` 等 (任意)
4. Upload image → skip でも OK
5. **Create**

### 1.3. Guild ID を取得 (後で `TASK_RELAY_DISCORD_GUILD_IDS` に入れる)

1. **User Settings (歯車)** → **Advanced** → **Developer Mode** を ON
2. サーバー左サイドバーでサーバーアイコン右クリック → **Copy Server ID** (数値、18-19 桁)
3. この ID をメモ

---

## 2. Bot Application の作成

### 2.1. Developer Portal で Application 作成

1. https://discord.com/developers/applications にアクセス (Discord ログイン必要)
2. 右上 **New Application**
3. Name: `task-relay` (任意)
4. **Create**

### 2.2. Bot 有効化 + Token 取得

1. 左サイドバーの **Bot**
2. Bot 設定画面で:
   - **Username**: `task-relay-bot` (表示名、任意)
   - **Icon**: 任意
3. **Privileged Gateway Intents** セクション:
   - **PRESENCE INTENT**: OFF (不要)
   - **SERVER MEMBERS INTENT**: OFF (不要)
   - **MESSAGE CONTENT INTENT**: OFF (不要 — slash command と DM のみ)
4. **TOKEN** セクション:
   - **Reset Token** → 表示される token をコピー (`TASK_RELAY_DISCORD_BOT_TOKEN` に使う)
   - ⚠️ token は一度しか表示されない。必ず即コピーして secret manager / 1Password / sops に保管
5. **Save Changes**

### 2.3. OAuth2 招待 URL 生成

1. 左サイドバー **OAuth2** → **URL Generator**
2. **SCOPES** にチェック:
   - `bot`
   - `applications.commands`
3. **BOT PERMISSIONS** にチェック:
   - `Send Messages` (DM 送信用)
   - `Use Slash Commands` (scope で自動付与されるので任意)
4. ページ下部の **GENERATED URL** をコピー
5. そのブラウザで URL を開く → **Add to Server** → step 1 で用意したサーバーを選択 → **Authorize**
6. reCAPTCHA → 完了

bot がサーバーに joined 状態になる (メンバーリスト右側に表示される)。

---

## 3. Admin User ID の取得

task-relay の `/unlock` / `/retry-system` は admin allowlist のみが使える。あなた自身 (運用者) の Discord user ID を取得:

1. 自分のアバターを右クリック → **Copy User ID** (数値、18-19 桁)
2. メモ (`TASK_RELAY_ADMIN_USER_IDS` に使う)

複数 admin が必要ならメンバー全員の User ID を同様にコピー (カンマ区切りで環境変数に入れる)。

---

## 4. task-relay.env への設定

task-relay を動かすホストで `/etc/task-relay/task-relay.env` に以下を設定する。開発時は `~/.config/task-relay/task-relay.env` に置き、`direnv` / `systemd-run --setenv-file` / `set -a; . ~/.config/task-relay/task-relay.env; set +a` などで明示的に読み込む。worktree 直下の `.env` は使わない。

```bash
# Discord
TASK_RELAY_DISCORD_BOT_TOKEN=<step 2.2 でコピーした token>
TASK_RELAY_DISCORD_GUILD_IDS=<step 1.3 でコピーした Guild ID>
# 複数 guild なら: TASK_RELAY_DISCORD_GUILD_IDS=111111111111111111,222222222222222222

# Admin allowlist
TASK_RELAY_ADMIN_USER_IDS=<step 3 でコピーした User ID>
# 複数 admin なら: TASK_RELAY_ADMIN_USER_IDS=111111111111111111,222222222222222222

# Discord writer / ack タイミング (default 値、変更不要)
TASK_RELAY_DISCORD_WRITER_QUEUE_CAPACITY=1024
TASK_RELAY_DISCORD_ACK_DEADLINE_MS=1500
```

⚠️ **token 流出に注意**:
- `task-relay.env` を git commit しない
- systemd `EnvironmentFile=/etc/task-relay/task-relay.env` は `chmod 600` に制限
- sops + age で暗号化 (`deploy/restore-drill.sh` と同じ方式)

---

## 5. 動作確認

### 5.1. Bot 起動 (development)

```bash
cd task-relay
uv run task-relay ingress-discord
```

bot が Discord に接続し、`<bot-name> is online` 相当のログが出る。

### 5.2. Slash command 登録確認

サーバーでメッセージ欄に `/` を打つと、task-relay の slash command が候補に出る:

- `/approve task_id:<id>`
- `/critical task_id:<id> on:True/False`
- `/retry task_id:<id> replan:True/False`
- `/cancel task_id:<id>`
- `/unlock branch:<name>`
- `/retry-system stage:<stage>`
- `/status`

出ない場合:
- `TASK_RELAY_DISCORD_GUILD_IDS` が正しいか確認
- bot が guild に join しているか確認
- `setup_hook` の tree.sync が失敗していないか log 確認

### 5.3. DM 送信確認 (projection --with-discord)

別 terminal で:

```bash
uv run task-relay projection --with-discord
```

bot が projection sink にも接続。test event を journal に投入して DM が届くか確認:

```bash
uv run task-relay cancel --task TEST --actor <your-discord-id>
```

ingester + router 経由で cancelled 遷移 → label_sync outbox (Forgejo) + もし Discord alert 対象なら DM 送信。

`--with-discord` flag 無しの場合は DM 送信が `NotImplementedError` で失敗するので、projection service には必ず flag を付ける (systemd unit は 2b385ef で対応済)。

---

## 6. トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| Slash command が Discord に出ない | guild ID 不一致 / tree.sync 失敗 | Developer Mode で guild ID 再確認、log で sync エラー確認 |
| Bot が online にならない | token 無効 / intent 不足 | Developer Portal で token reset、intent を `Intents.default()` に戻す |
| DM が届かない | privacy setting "Allow direct messages from server members" が OFF | 受信者 → User Settings → Privacy & Safety → ON |
| `Bot is not in target guild` エラー | bot 未招待 / kicked | OAuth2 URL で再招待 |
| 429 Rate limit | token 共有 / excessive calls | token を task-relay 専用に分離、writer queue capacity 確認 |

---

## 7. チェックリスト (I1 実機結合検証の前)

- [ ] Guild ID (`TASK_RELAY_DISCORD_GUILD_IDS`) 取得済
- [ ] Bot Token (`TASK_RELAY_DISCORD_BOT_TOKEN`) 取得済、secret manager に保管
- [ ] Admin User ID(s) (`TASK_RELAY_ADMIN_USER_IDS`) 取得済
- [ ] Bot がサーバーに join 済 (メンバーリストに表示)
- [ ] Privileged intent すべて OFF
- [ ] `task-relay.env` に 3 設定を記載
- [ ] `uv run task-relay ingress-discord` で bot online 確認
- [ ] Slash command `/status` が Discord で呼べる
- [ ] `uv run task-relay projection --with-discord` が失敗せず起動
- [ ] test task を投入し DM が届くことを確認

---

## 8. セキュリティ注意

- **token は個人情報級の secret**。漏洩時は Developer Portal で即 **Reset Token**
- bot を **public bot** にしない (Developer Portal → Bot → Public Bot = OFF)
- `TASK_RELAY_ADMIN_USER_IDS` に記載した user のみ `/unlock` `/retry-system` 実行可能
- DM 内容は Discord server 運営が読める (E2EE なし)。`basic-design §9.4` の情報境界で task_id / state / URL のみに制限済
- sops + age 暗号化の secret rotation 手順は `docs/reference/runbook.md` 参照

---

## 付録: Discord API の関連 docs

- [Discord Developer Portal](https://discord.com/developers/applications)
- [Discord Gateway Intents](https://discord.com/developers/docs/topics/gateway#gateway-intents)
- [discord.py Documentation](https://discordpy.readthedocs.io/en/stable/)
- [OAuth2 scopes reference](https://discord.com/developers/docs/topics/oauth2#shared-resources-oauth2-scopes)
