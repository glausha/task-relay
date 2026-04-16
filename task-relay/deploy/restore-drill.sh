#!/usr/bin/env bash
set -euo pipefail

# detailed-design §13.4 Restore 手順の自動化
# Usage: ./deploy/restore-drill.sh [--max-lag-seconds <seconds>] <sqlite_path> <journal_dir>

MAX_LAG_SECONDS=""
while (($# > 0)); do
  case "$1" in
    --max-lag-seconds)
      MAX_LAG_SECONDS="${2:?usage: restore-drill.sh [--max-lag-seconds <seconds>] <sqlite_path> <journal_dir>}"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "usage: restore-drill.sh [--max-lag-seconds <seconds>] <sqlite_path> <journal_dir>" >&2
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

SQLITE_PATH="${1:?usage: restore-drill.sh [--max-lag-seconds <seconds>] <sqlite_path> <journal_dir>}"
JOURNAL_DIR="${2:?usage: restore-drill.sh [--max-lag-seconds <seconds>] <sqlite_path> <journal_dir>}"

export TASK_RELAY_SQLITE_PATH="$SQLITE_PATH"
export TASK_RELAY_JOURNAL_DIR="$JOURNAL_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Step 0: secrets-decrypt ==="
# basic-design §9.3 / docs/guides/secret-management.md §11: restore 時に secret を復元
# root 権限かつ deploy/secrets/ が存在する場合のみ実行 (test/dev 環境では skip)
if [[ -x "${SCRIPT_DIR}/secrets-decrypt.sh" && "${EUID}" -eq 0 && -d "${SCRIPT_DIR}/secrets" ]]; then
    "${SCRIPT_DIR}/secrets-decrypt.sh" --force
else
    echo "restore-drill: secrets-decrypt skipped (non-root, or deploy/secrets/ missing)" >&2
fi

echo "=== Step 1: db-check ==="
task-relay db-check

echo "=== Step 2: journal-replay ==="
task-relay journal-replay

echo "=== Step 3: reconcile ==="
task-relay reconcile || true

echo "=== Step 4: health-check ==="
task-relay health-check

echo "=== Step 5: success criteria ==="
if [[ -n "$MAX_LAG_SECONDS" ]]; then
  if [[ -n "${TASK_RELAY_JOURNAL_OFFSITE_LAG_SECONDS:-}" ]]; then
    if (( TASK_RELAY_JOURNAL_OFFSITE_LAG_SECONDS > MAX_LAG_SECONDS )); then
      echo "restore-drill: journal_offsite_lag_seconds=${TASK_RELAY_JOURNAL_OFFSITE_LAG_SECONDS} exceeds ${MAX_LAG_SECONDS}" >&2
      exit 1
    fi
  else
    echo "restore-drill: skipping journal_offsite_lag_seconds check; metric unavailable" >&2
  fi
fi

# replication_lag / snapshot freshness remain externally validated from Litestream metrics.
echo "restore-drill: PASS"
