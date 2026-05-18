#!/usr/bin/env bash
# cus SubagentStop hook — track subagent completion.
#
# Pair with cus_pre_tool_use.sh for subagent-skip-guard logic in Phase 5.
# When a subagent (Task tool) finishes, we want the daemon to know so it
# can proceed with deferred swaps.
#
# Appends one line per subagent-stop to ~/claude-accounts/tool_use.log:
#   <ts>,<session_id>,subagent,stop
#
# Walk-back: removing the hook entry disables completion tracking. Daemon
# falls back to time-based heuristics ("if PreToolUse line was N seconds
# ago, assume it's done").

set -euo pipefail

ACCOUNTS_DIR="${CUS_ACCOUNTS_DIR:-$HOME/claude-accounts}"
LOG="$ACCOUNTS_DIR/tool_use.log"

EVENT=$(cat || true)
SESSION_ID=$(echo "$EVENT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
mkdir -p "$ACCOUNTS_DIR"
echo "$TS,$SESSION_ID,subagent,stop" >> "$LOG"

exit 0
