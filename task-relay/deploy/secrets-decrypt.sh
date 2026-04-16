#!/usr/bin/env bash
set -euo pipefail

# task-relay secret decryption
# sops + age 暗号化された secret を /etc/task-relay/ に復号配置。
# basic-design §9.3 に準拠 (env 注入のみ、worktree 配置禁止)。
# Usage: sudo ./deploy/secrets-decrypt.sh [--force]
#   --force: 既存の平文ファイルを上書きする (default は既存があれば skip)

APP_USER="task-relay"
APP_GROUP="task-relay"
ENV_DIR="/etc/task-relay"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SECRETS_DIR="${REPO_ROOT}/deploy/secrets"
FORCE=0

while (($# > 0)); do
    case "$1" in
        --force) FORCE=1; shift ;;
        -h|--help)
            sed -n '3,10p' "$0"
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "secrets-decrypt.sh must be run as root" >&2
    exit 1
fi

if ! command -v sops >/dev/null 2>&1; then
    echo "sops is required but was not found in PATH" >&2
    echo "install: curl -L https://github.com/getsops/sops/releases/latest/download/sops-v3.9.0.linux.amd64 -o /tmp/sops && install -m 755 /tmp/sops /usr/local/bin/sops" >&2
    exit 1
fi

# age 秘密鍵が見つからない場合は sops が decrypt 時に明確なエラーを出す。
# SOPS_AGE_KEY_FILE が未設定なら既定の場所を試す (root 運用前提)。
if [[ -z "${SOPS_AGE_KEY_FILE:-}" ]]; then
    for candidate in /etc/task-relay/age-keys.txt /root/.config/sops/age/keys.txt "${HOME}/.config/sops/age/keys.txt"; do
        if [[ -f "$candidate" ]]; then
            export SOPS_AGE_KEY_FILE="$candidate"
            break
        fi
    done
fi

if [[ ! -d "$SECRETS_DIR" ]]; then
    echo "secrets dir not found: $SECRETS_DIR" >&2
    exit 1
fi

install -d -o root -g "${APP_GROUP}" -m 0750 "${ENV_DIR}"

umask 077

decrypt_one() {
    local src="$1"
    local dst="$2"
    if [[ ! -f "$src" ]]; then
        echo "secrets-decrypt: source missing, skip: $src" >&2
        return 0
    fi
    if [[ -f "$dst" && "$FORCE" -ne 1 ]]; then
        echo "secrets-decrypt: target exists, skip (use --force to overwrite): $dst" >&2
        return 0
    fi
    local tmp
    tmp="$(mktemp)"
    # mktemp is 600 by default; decrypt into tmp then move atomically
    if ! sops --decrypt "$src" >"$tmp"; then
        rm -f "$tmp"
        echo "secrets-decrypt: decryption failed for $src" >&2
        exit 1
    fi
    install -m 0600 -o "${APP_USER}" -g "${APP_GROUP}" "$tmp" "$dst"
    shred -u "$tmp" 2>/dev/null || rm -f "$tmp"
    echo "secrets-decrypt: wrote $dst"
}

decrypt_one "${SECRETS_DIR}/task-relay.env" "${ENV_DIR}/task-relay.env"
decrypt_one "${SECRETS_DIR}/litestream.yml" "${ENV_DIR}/litestream.yml"

echo "secrets-decrypt: complete"
