#!/usr/bin/env bash
# cus PostToolUseFailure hook — detect rate-limit / 429 in error bodies.
#
# Pattern lifted from cux (internal/hooks/hooks.go:765-800): substring-match
# the error body for rate-limit indicators and write a signal file the daemon
# reads. Daemon reacts immediately without waiting for the next poll.
#
# Appends one line per detection to ~/claude-accounts/429.log:
#   <ts>,<session_id>,<matched_substring>
#
# Walk-back: removing the hook entry disables reactive swaps. The daemon
# still triggers proactive swaps on threshold crossing — just slower.

set -euo pipefail

ACCOUNTS_DIR="${CUS_ACCOUNTS_DIR:-$HOME/claude-accounts}"
LOG="$ACCOUNTS_DIR/429.log"

EVENT=$(cat || true)

# Concatenate the whole event for substring matching (cheap; events are small)
EVENT_LOWER=$(echo "$EVENT" | tr '[:upper:]' '[:lower:]')

MATCHED=""
for pattern in "rate_limit" "rate limit" "usage limit" "overloaded_error" "rate-limit" "ratelimit"; do
    if echo "$EVENT_LOWER" | grep -qF "$pattern"; then
        MATCHED="$pattern"
        break
    fi
done

if [[ -z "$MATCHED" ]]; then
    exit 0
fi

SESSION_ID=$(echo "$EVENT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

mkdir -p "$ACCOUNTS_DIR"
echo "$TS,$SESSION_ID,$MATCHED" >> "$LOG"

exit 0
