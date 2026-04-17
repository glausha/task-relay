# task-relay Runbook

この runbook は deploy 資材と systemd 運用の最短手順をまとめたリポジトリ内参照です。
設計上の source of truth は親ディレクトリの `detailed-design-v1.0.md` と `basic-design-v1.0.md` を参照してください。

## Startup Order

`detailed-design` §11.1 の標準起動順:

1. `redis.service`
2. `task-relay-db-check.service`
3. `task-relay-journal-replay.service`
4. `task-relay-journal-ingester.service`
5. `task-relay-reconcile.service`
6. `task-relay-router.service`
7. `task-relay-discord-bot.service`
8. `task-relay-projection.service`
9. `task-relay-retention.service`

補足:
- Forgejo は `TASK_RELAY_FORGEJO_BASE_URL` で到達できればよく、`task-relay.target` はローカル `forgejo.service` を pull しない。
- 新規に Forgejo を立てる場合は `docker compose --env-file /etc/task-relay/forgejo-compose.env -f deploy/forgejo-compose.yml up -d` を先に実行する。
- `task-relay-journal-replay.service` は起動時 replay 専用です。
- `task-relay-journal-ingester.service` は継続 ingest 専用です。
- `task-relay-reconcile.service` は state を直接書き換えず、`internal.reconcile_resume` を journal に append します。
- `task-relay-forgejo-webhook.service` は `task-relay-router.service` 後に並行起動します。
- `task-relay-retention.service` は常駐せず、`task-relay-retention.timer` が日次で起動します。

## Systemd

初回導入:

```bash
sudo cp deploy/.env.example /etc/task-relay/task-relay.env
sudoedit /etc/task-relay/task-relay.env
sudo bash deploy/install.sh
```

環境変数メモ:
- `task-relay-projection.service` は `task-relay projection --with-discord` で起動するため、Discord DM alert を実送信する環境では `TASK_RELAY_DISCORD_BOT_TOKEN` を `/etc/task-relay/task-relay.env` に設定する。
- token 未設定でも service 自体は起動し、projection worker は warning を出して Discord client なしで継続する。この場合 `discord_alert` の実送信は行われない。

起動:

```bash
docker compose --env-file /etc/task-relay/forgejo-compose.env -f deploy/forgejo-compose.yml up -d  # 新規 Forgejo の場合のみ
sudo systemctl start task-relay.target
sudo systemctl status task-relay.target
```

`systemctl start task-relay.target` の挙動:
- `task-relay.target` が db-check から projection までの chain を pull します。
- `After=` 関係により `db-check -> journal-replay -> journal-ingester -> reconcile -> router` の順で並びます。
- `discord-bot`, `forgejo-webhook`, `projection` は router 起動後に常駐 service として立ち上がります。
- retention は target 配下で timer を有効化し、日次の `task-relay-retention.service` 実行に任せます。

よく使う確認:

```bash
sudo systemctl status task-relay-db-check.service
sudo systemctl status task-relay-router.service
sudo systemctl status task-relay-discord-bot.service
sudo systemctl status task-relay-forgejo-webhook.service
sudo systemctl status task-relay-projection.service
sudo systemctl list-timers task-relay-retention.timer
```

手動実行:

```bash
sudo systemctl start task-relay-retention.service
sudo systemctl restart task-relay-router.service
sudo journalctl -u task-relay-router.service -n 100 --no-pager
```

## Recovery Notes

- 起動失敗時は `task-relay-db-check.service` から順に `systemctl status` と `journalctl -u <unit>` を確認します。
- SQLite restore 後は `deploy/restore-drill.sh <sqlite_path> <journal_dir>` を実行し、`db-check -> journal-replay -> reconcile -> health-check` を再確認します。
- breaker や queue stuck の admin 介入は、systemd 再起動より前に原因と影響範囲を確認してから実施します。
