# Journal — 2026-05-18 (claude-usage-swap planning)

## Context

Initial planning arc for a new tool that monitors Claude Code account usage and auto-rotates the active OAuth identity by file-copy swap when configurable thresholds are crossed. The user wanted a build plan produced via AVC methodology (and gym integration if available — gym MCP was disconnected so AVC alone).

The arc spans architecture survey (existing tools), mechanism validation (where auth actually lives on Linux), design decisions (lift-not-fork; file-copy vs CLAUDE_CONFIG_DIR; progressive per-account thresholds 50→75→90→force), and phase boundaries (Phase 1+2 for v0.1, hot-swap deferred to Phase 3+). Triggered by conversational request "How can we configure Claude Code to switch accounts as it sees usage approaching the limit?"

## What shipped this session

### Methodology framing

- AVC pipeline overview, plan template, journal template, inbox template, autonomous-collaboration doc read end-to-end before producing artifacts.
- AVC discipline hook verified installed (`/home/rayi/.claude/hooks/avc_methodology_hook.sh` ✓, settings.json reference ✓, Tier 2 LLM-review gated by env var ✓).
- Gym MCP unavailable this session — flagged in inbox, plan written under AVC alone.

### Architecture survey

- Two parallel research agents dispatched: one for cux internals (returned full file:line map of reusable vs replaceable parts), one for broader account-rotation landscape (returned 6 tools; verdict: gap is real, no exact match exists).
- Tools evaluated: cux (Go wrapper, in-place cred swap, single threshold — closest match but overengineered for our case), teamclaude (proxy), AIMUX (manual CLAUDE_CONFIG_DIR switcher), ccflare (proxy with dashboard, 972 stars), claude-code-hub (Postgres+Redis, team-scale), claudeusage-mcp (signal source only).
- Verified: `--dangerously-skip-permissions` does NOT bypass hooks (Claude Code authentication + permission-modes docs). Cross-`CLAUDE_CONFIG_DIR` `--resume` works. `claude --resume` waits for prompt — wake-up message required.

### Mechanism validation

- Direct inspection of `~/.claude/.credentials.json` and `~/.claude-merkos/.credentials.json` confirmed Linux auth is a single 471-byte file with structure `{claudeAiOauth: {accessToken, refreshToken, expiresAt, scopes, subscriptionType, rateLimitTier}}`. No OS-keystore involvement on Linux per official docs.
- Direct inspection of `~/.claude.json` keys (39 top-level) confirmed `oauthAccount` block + `userID` are the account-bound fields; rest is caches/stats.
- Conclusion: minimum swap footprint is two files totaling ~37 KB. Sub-millisecond atomic copy via tempfile+rename.

### Artifacts produced

- `docs/plans/2026-05-18-claude-usage-swap.md` — full build plan, 6 phases with parallelism map, 7 open questions, explicit out-of-scope list.
- `docs/journal/2026-05-18-claude-usage-swap-planning.md` — this file.
- `inbox.md` — 3 entries (1 flag, 2 decisions including 1 ARCH DECISION).
- `~/repos/claude-usage-swap/` — git repo initialized, no commits yet (awaiting user authorization).

## Design calls worth preserving

**File-copy swap, not `CLAUDE_CONFIG_DIR` env-var swap.** AIMUX uses env-var swap; the user's existing `~/.claude-merkos/` follows the same pattern. We deliberately chose file-copy because: (a) existing tmux/shell launches work without wrapper or alias plumbing; (b) one canonical `~/.claude/` location matches user mental model; (c) trade-off is no concurrent multi-account on one machine — which the user said they don't need. Documented in plan §"Decision history" and inbox entry on architecture.

**Progressive per-account thresholds (50→75→90→force).** Novel vs cux/AIMUX. User's framing: "swap to account B at 50% on account A, then again after we swapped back to account A at 50 on account B swap to account B again at 75%." Per-account state machine in `state.json` tracks `next_swap_at_pct` per account; climbs after each return. Yields natural load-balancing across N accounts without needing a separate scheduler.

**Lift cux patterns, do not fork.** Patterns lifted: Stop-hook turn-boundary signaling, PostToolUseFailure 429 substring-match, signature-keyed hook installer, `--resume <id> "Go continue."` wake-up. NOT lifted: cux's keystore-swap and transactional rollback (unnecessary on Linux because `.credentials.json` IS the keystore). Python implementation will reference origin file:line in comments for attribution. Inbox ARCH DECISION entry has full reasoning + walk-back.

**v0.1 = Phase 1+2 only.** Hot-swap deferred. Validate per-account state machine on real workload for several days before adding tmux pane registry, Stop hook tailing, and pause-message injection. 80/20 reasoning: new-session swap delivers most user-visible value; hot-swap adds clustered risk.

**AVC's plan_template adopted for an engineering project.** AVC is primarily research/ML-oriented in practice, but the plan_template (north star + phases with parallelism map + out-of-scope + open questions) maps well to engineering work. The committee/red-team stages were skipped because the prior conversation already surfaced the relevant objections (cux overengineering, cache-bust tax, subagent-skip scenarios, fast-loop sessions).

## Hand-off — Phase 1 ready to start

Next worker (likely same session or next iteration) picks up Phase 1.1 (account-file inventory):

1. Diff `~/.claude.json` against `~/.claude-merkos/.claude.json`. Document differing keys.
2. Confirm the differing keys are exactly `oauthAccount`, `userID`, possibly some cache fields that are safe to swap. If extra non-account state shows up, escalate to plan open-question #2 (surgical key-merge vs whole-file swap).
3. Output: short `docs/ARCHITECTURE.md` noting the canonical account-bound file set.

After 1.1 lands, 1.2 (`cus init` migration script) and 1.3 (`cus switch` manual swap CLI) are independent enough that they could be split across two sessions if useful. Phase 2 starts only after Phase 1 is stable on real workload (recommend ≥48 hours of manual swap usage with no corruption before automating).

Concrete first command after this planning arc:

```bash
python3 -c "
import json
a = json.load(open('/home/rayi/.claude.json'))
b = json.load(open('/home/rayi/.claude-merkos/.claude.json'))
diffs = sorted(k for k in set(a)|set(b) if a.get(k) != b.get(k))
print('Keys that differ:', diffs)
"
```

## Non-blocking flags for future work

- **Gym MCP integration revisit when reconnected.** Open plan question #6. Likely candidates: `gym_drill` for stress-testing swap atomicity, `gym_audit_coding` for code review at end of Phase 2.
- **Cross-machine coordination.** Explicitly out of scope for v1. If the user starts running `cus daemon` on more than one machine simultaneously, two daemons could pick the same swap target and double-burn an account. Park as future GH issue.
- **OAuth refresh during swap window.** If the active account's `accessToken` is mid-refresh when we swap (the token file is being rewritten by Claude Code), we could lose the refresh. Mitigation in Phase 1.3: read+verify token file integrity before saving back to account dir; retry if mid-write.
- **Stats-cache + policy-limits not swapped (v0.1).** AIMUX isolates these; we don't. May cause confusing stats display in Claude Code itself after a swap. Watch for during Phase 2 testing; add to swap list if it matters.

## Numbers

- Plan: 1 file, ~220 lines, 6 phases + 7 open questions
- Journal: 1 file (this one)
- Inbox entries: 3 (1 flag + 2 decisions)
- Research agents dispatched: 5 (2 cux/competitor surveys + 1 AIMUX mechanism + 1 deep alternatives + 1 verification of dangerouslySkipPermissions)
- Bash inspections of local config: 4 (~/.claude/ structure, ~/.claude-merkos/ structure, credentials file structures, claude.json key list)
- Tools used: AVC (`check_hook_status`, `start_journal`, `post_inbox_entry` ×3), TaskCreate ×5, TaskUpdate ×4, Bash, Read, Write, Edit, Agent
- Effort estimate produced for the actual build: ~16-22 hours across Phases 1-5; ~3 hours for Phase 6 if shipped in parallel
