#!/usr/bin/env bash
# cus PreToolUse hook — track active tool calls for subagent-skip-guard.
#
# Phase 5: before triggering a swap on a session, we check whether it has
# an active subagent (Task tool currently running) or long-running shell.
# This hook increments a per-session counter; SubagentStop and a synthetic
# "tool_use completed" signal decrement it.
#
# Appends one line per tool-use start to ~/claude-accounts/tool_use.log:
#   <ts>,<session_id>,<tool_name>,start
#
# The daemon scans this log + matching completions to estimate "in-flight
# tool calls per session." Imperfect but cheap; good enough for skip-guard.
#
# Walk-back: removing the hook entry disables the skip-guard. Daemon will
# proceed with swaps even when subagents are active, which is what cux does.

set -euo pipefail

ACCOUNTS_DIR="${CUS_ACCOUNTS_DIR:-$HOME/claude-accounts}"
LOG="$ACCOUNTS_DIR/tool_use.log"

EVENT=$(cat || true)
SESSION_ID=$(echo "$EVENT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")
TOOL=$(echo "$EVENT" | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || echo "unknown")

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
mkdir -p "$ACCOUNTS_DIR"
echo "$TS,$SESSION_ID,$TOOL,start" >> "$LOG"

exit 0
