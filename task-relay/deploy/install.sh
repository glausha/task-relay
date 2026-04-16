#!/usr/bin/env bash
set -euo pipefail

# task-relay install script
# Assumes the repository checkout is already staged at /var/lib/task-relay.

APP_USER="task-relay"
APP_GROUP="task-relay"
APP_DIR="/var/lib/task-relay"
ENV_DIR="/etc/task-relay"
SYSTEMD_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_BIN="${APP_DIR}/.venv/bin/task-relay"

if [[ "${EUID}" -ne 0 ]]; then
    echo "install.sh must be run as root" >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required but was not found in PATH" >&2
    exit 1
fi

if [[ "${REPO_ROOT}" != "${APP_DIR}" ]]; then
    echo "install.sh must be run from a checkout located at ${APP_DIR}" >&2
    echo "current repo root: ${REPO_ROOT}" >&2
    exit 1
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi

if ! getent group "${APP_GROUP}" >/dev/null 2>&1; then
    groupadd --system "${APP_GROUP}"
    usermod -g "${APP_GROUP}" "${APP_USER}"
fi

install -d -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}"
install -d -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}/journal"
install -d -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}/logs"
install -d -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}/worktrees"
install -d -o root -g root -m 0750 "${ENV_DIR}"

chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"

runuser -u "${APP_USER}" -- uv sync --directory "${APP_DIR}"

ln -sfn "${VENV_BIN}" /usr/local/bin/task-relay

install -m 0644 "${REPO_ROOT}/deploy/systemd/"*.service "${SYSTEMD_DIR}/"
install -m 0644 "${REPO_ROOT}/deploy/systemd/"*.timer "${SYSTEMD_DIR}/"
install -m 0644 "${REPO_ROOT}/deploy/systemd/task-relay.target" "${SYSTEMD_DIR}/task-relay.target"

# basic-design §9.3: secret は sops+age 暗号化から env/litestream.yml に復号配置
if [[ -f "${REPO_ROOT}/deploy/secrets/task-relay.env" \
    || -f "${REPO_ROOT}/deploy/secrets/litestream.yml" ]]; then
    # age 秘密鍵のバックアップ確認を enforce (persona-advocate overriding blocker)
    # WHY: age 秘密鍵を失うと全 secret 再発行 30-60 分。backup 未実施での systemctl start を防ぐ
    _age_key="${SOPS_AGE_KEY_FILE:-}"
    for candidate in /etc/task-relay/age-keys.txt /root/.config/sops/age/keys.txt "${HOME:-/root}/.config/sops/age/keys.txt"; do
        if [[ -z "$_age_key" && -f "$candidate" ]]; then
            _age_key="$candidate"
        fi
    done
    if [[ -n "$_age_key" && -f "$_age_key" ]]; then
        echo "install.sh: age secret key detected at $_age_key"
        echo "install.sh: BACKUP is REQUIRED (1Password vault / paper + 施錠). GitHub/cloud 禁止."
        echo "install.sh: loss → 全 secret 再発行 30-60 分 downtime (disaster-recovery.md §8.1)"
        if [[ -t 0 && -z "${TASK_RELAY_INSTALL_SKIP_AGE_BACKUP_CHECK:-}" ]]; then
            read -r -p "install.sh: age key backup 済みか? [y/N]: " _ack
            case "$_ack" in
                y|Y|yes|YES) ;;
                *) echo "install.sh: abort. docs/guides/secret-management.md §2.3 で backup 後に再実行" >&2; exit 1 ;;
            esac
        else
            echo "install.sh: non-interactive run; TASK_RELAY_INSTALL_SKIP_AGE_BACKUP_CHECK=1 set or stdin not tty — backup を外部で確認済前提で進行" >&2
        fi
    fi
    "${SCRIPT_DIR}/secrets-decrypt.sh"
else
    echo "install.sh: deploy/secrets/ not found; skip decryption (first run or secrets managed externally)" >&2
fi

systemctl daemon-reload
systemctl enable task-relay.target
systemctl enable task-relay-retention.timer

echo "task-relay install: complete"
