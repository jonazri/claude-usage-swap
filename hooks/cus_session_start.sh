#!/usr/bin/env bash
# cus SessionStart hook — log new sessions for visibility + tmux pane registry.
#
# Reads the SessionStart hook JSON event from stdin (per Claude Code hooks spec):
#   {"session_id": "...", "transcript_path": "...", "cwd": "...", ...}
# Writes one line per session to ~/claude-accounts/sessions.log:
#   <ts>,<session_id>,<account>,<tmux_pane>,<cwd>
#
# Account is whichever is active in state.json at the moment this fires.
# TMUX_PANE comes from the env (set by tmux if running under it).
#
# Walk-back: this hook only writes to a log file. Removing the hook entry
# from ~/.claude/settings.json reverts visibility. The log itself is
# safe to delete.

set -euo pipefail

ACCOUNTS_DIR="${CUS_ACCOUNTS_DIR:-$HOME/claude-accounts}"
LOG="$ACCOUNTS_DIR/sessions.log"
STATE="$ACCOUNTS_DIR/state.json"

# Read the JSON event from stdin (Claude Code passes hooks data this way)
EVENT=$(cat || true)

# Extract session_id and cwd via grep+sed (avoid jq dep)
SESSION_ID=$(echo "$EVENT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")
CWD=$(echo "$EVENT" | grep -o '"cwd"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")

# Active account from state.json (best-effort grep)
ACCOUNT="unknown"
if [[ -f "$STATE" ]]; then
    ACCOUNT=$(grep -o '"active"[[:space:]]*:[[:space:]]*"[^"]*"' "$STATE" | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")
fi

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
PANE="${TMUX_PANE:-no-tmux}"

mkdir -p "$ACCOUNTS_DIR"
echo "$TS,$SESSION_ID,$ACCOUNT,$PANE,$CWD" >> "$LOG"

# Hooks should not block the session — exit 0 always.
exit 0
