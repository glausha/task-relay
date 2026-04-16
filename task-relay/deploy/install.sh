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

systemctl daemon-reload
systemctl enable task-relay.target
systemctl enable task-relay-retention.timer

echo "task-relay install: complete"
