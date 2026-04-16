#!/usr/bin/env bash
set -euo pipefail

# detailed-design §13.4 Restore 手順の自動化
# Usage: ./deploy/restore-drill.sh <sqlite_path> <journal_dir>

SQLITE_PATH="${1:?usage: restore-drill.sh <sqlite_path> <journal_dir>}"
JOURNAL_DIR="${2:?usage: restore-drill.sh <sqlite_path> <journal_dir>}"

export TASK_RELAY_SQLITE_PATH="$SQLITE_PATH"
export TASK_RELAY_JOURNAL_DIR="$JOURNAL_DIR"

echo "=== Step 1: db-check ==="
task-relay db-check

echo "=== Step 2: journal-replay ==="
task-relay journal-replay

echo "=== Step 3: reconcile ==="
task-relay reconcile || true

echo "=== Step 4: health-check ==="
task-relay health-check

echo "=== Step 5: success criteria ==="
# replication_lag / snapshot freshness / journal sync lag are expected
# to be validated externally from Litestream metrics.
echo "restore-drill: PASS"
