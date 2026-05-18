#!/usr/bin/env bash
# cus Stop hook — heartbeat at end of each model-response turn.
#
# Used by the daemon to:
#   - Detect turn boundaries for Tier 1 hot-swap (wait-for-Stop).
#   - Detect liveness of sessions (latest stop ts per session_id).
#
# Reads the Stop hook JSON event from stdin and appends one line per Stop to
# ~/claude-accounts/stops.log:
#   <ts>,<session_id>
#
# Walk-back: removing the hook entry from ~/.claude/settings.json disables
# turn-boundary detection. The daemon falls back to wait-with-timeout.

set -euo pipefail

ACCOUNTS_DIR="${CUS_ACCOUNTS_DIR:-$HOME/claude-accounts}"
LOG="$ACCOUNTS_DIR/stops.log"

EVENT=$(cat || true)
SESSION_ID=$(echo "$EVENT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
mkdir -p "$ACCOUNTS_DIR"
echo "$TS,$SESSION_ID" >> "$LOG"

exit 0
