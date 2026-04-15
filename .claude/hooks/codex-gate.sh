#!/usr/bin/env bash
# CodexGate PreToolUse hook
# task-relay 配下への Write / Edit を検知したら、ツール呼び出しをブロックし
# 代わりに codex CLI を呼んで Codex に実際の書き込みを行わせる。
#
# Claude Code PreToolUse hook 仕様:
#   stdin: JSON ({tool_name, tool_input, ...})
#   exit 0                                -> そのままツール実行
#   exit 2                                -> ツール実行を拒否 (stderr をモデルに返す)
#   stdout に {"decision":"block","reason":"..."} を出力 -> 実行を止め、reason をモデルに返す

set -euo pipefail

LOG="${CLAUDE_PROJECT_DIR:-/home/akala/Documents/Glauca/task-relay}/.claude/hooks/codex-gate.log"
mkdir -p "$(dirname "$LOG")"

payload="$(cat)"
tool_name="$(printf '%s' "$payload" | jq -r '.tool_name // empty')"

# Write / Edit 以外は無条件許可
case "$tool_name" in
  Write|Edit) ;;
  *) exit 0 ;;
esac

file_path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty')"

# スコープ外 (task-relay 配下以外) は許可
case "$file_path" in
  */task-relay/*) ;;
  *) exit 0 ;;
esac

# .claude/ 配下の設定ファイル編集はゲート運用上そのまま許可
case "$file_path" in
  */task-relay/.claude/*) exit 0 ;;
esac

# バイパス 1: 明示フラグ
if [ "${CODEX_GATE_APPLY:-0}" = "1" ]; then
  exit 0
fi

# バイパス 2: Codex エージェント配下から呼ばれた場合 (無限ループ防止)
agent_ctx="${CLAUDE_AGENT_TYPE:-}${CLAUDE_SUBAGENT_TYPE:-}${CLAUDECODE_AGENT:-}${CODEX_GATE_INFLIGHT:-}"
if printf '%s' "$agent_ctx" | grep -qi 'codex\|inflight'; then
  exit 0
fi

# ---- ここから Codex へ委譲 ----

ts="$(date '+%Y-%m-%d %H:%M:%S')"
echo "[$ts] gate triggered: tool=$tool_name file=$file_path" >> "$LOG"

# プロンプト生成: Write / Edit それぞれ Codex に与える指示文を作る
prompt_file="$(mktemp)"
trap 'rm -f "$prompt_file"' EXIT

if [ "$tool_name" = "Write" ]; then
  content="$(printf '%s' "$payload" | jq -r '.tool_input.content // ""')"
  {
    echo "Write the following content to the file \`$file_path\` exactly as given."
    echo "Create parent directories if needed. Preserve content verbatim (no reformatting)."
    echo "Do not modify any other file. Do not run tests or builds."
    echo
    echo "=== BEGIN CONTENT ==="
    printf '%s' "$content"
    echo
    echo "=== END CONTENT ==="
  } > "$prompt_file"
else
  old_string="$(printf '%s' "$payload" | jq -r '.tool_input.old_string // ""')"
  new_string="$(printf '%s' "$payload" | jq -r '.tool_input.new_string // ""')"
  replace_all="$(printf '%s' "$payload" | jq -r '.tool_input.replace_all // false')"
  {
    echo "Edit the file \`$file_path\`."
    echo "Replace the exact string in <old> with the exact string in <new>."
    echo "replace_all=$replace_all."
    echo "Do not modify any other file. Preserve indentation and surrounding content."
    echo
    echo "<old>"
    printf '%s' "$old_string"
    echo
    echo "</old>"
    echo
    echo "<new>"
    printf '%s' "$new_string"
    echo
    echo "</new>"
  } > "$prompt_file"
fi

# codex exec を workspace-write サンドボックスで実行
# stdin から prompt を渡す
export CODEX_GATE_INFLIGHT=1
codex_log="$(mktemp)"
if codex exec -s danger-full-access --skip-git-repo-check - < "$prompt_file" > "$codex_log" 2>&1; then
  summary="$(tail -n 20 "$codex_log" | tr '\n' ' ' | cut -c1-400)"
  echo "[$ts] codex OK: $summary" >> "$LOG"
  # Claude には「ツール呼び出しは止めた。ただし Codex が実ファイルを書いた」と伝える
  jq -n --arg reason "CodexGate: this Write/Edit was forwarded to Codex and applied directly to $file_path. Do NOT retry the tool — the change is already on disk. Verify with Read if needed. Codex summary: $summary" \
    '{decision:"block", reason:$reason}'
  rm -f "$codex_log"
  exit 0
else
  err="$(tail -n 30 "$codex_log")"
  echo "[$ts] codex FAIL: $err" >> "$LOG"
  cat >&2 <<EOF
[CodexGate] Forwarding to Codex FAILED for $file_path.
Codex output (tail):
$err

Options:
  - Retry by calling the codex:codex-rescue agent directly.
  - Bypass the gate for this one write by setting CODEX_GATE_APPLY=1.
EOF
  rm -f "$codex_log"
  exit 2
fi
