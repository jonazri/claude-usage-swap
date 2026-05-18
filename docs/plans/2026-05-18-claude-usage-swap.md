# Plan — claude-usage-swap (2026-05-18)

## North star

A small daemon that watches Claude Code's per-account usage (5-hour and weekly windows) and automatically rotates the active OAuth identity by file-copy swap of `~/.claude/.credentials.json` + `~/.claude.json`, escalating thresholds per-account (50% → 75% → 90% → force) so that across an N-account pool we balance burn rather than hammering one account. Success looks like: a user with three or more accounts can run Claude Code continuously and never manually log in/out — the daemon picks the right account for each new session, and (in later phases) hot-swaps in-flight sessions when needed without losing conversation context.

## Context / background

**Why now.** The user manages multiple Claude Pro/Max accounts, multiple machines, and many concurrent Claude Code sessions. Hitting the 5-hour or weekly cap mid-task is a routine occurrence; the manual remedy (`/exit`, set a different `CLAUDE_CONFIG_DIR`, relaunch with `--resume`) is friction. Two existing tools (cux, AIMUX) solve adjacent parts of the problem but neither delivers progressive thresholds + tmux-aware hot-swap + multi-account pool together.

**Decision history (this session).** The planning conversation walked through:

1. **Existing-tool survey** (parallel research agents) — closest matches are [`cux`](https://github.com/inulute/cux) (Go wrapper around `claude`, in-place credential swap, single global threshold), [`teamclaude`](https://github.com/KarpelesLab/teamclaude) (proxy-based rotation), and [`AIMUX`](https://github.com/Digital-Threads/aimux) (manual `CLAUDE_CONFIG_DIR` profile switcher). [`ccflare`](https://github.com/snipeship/claude-balancer) and [`claude-code-hub`](https://github.com/ding113/claude-code-hub) are heavier proxy/team-scale offerings. None ship progressive per-account thresholds for solo use over file-copy swap.
2. **Architecture choice — lift-not-fork.** cux's credential-swap (`internal/switcher/switcher.go`) is overengineered for our case because it manipulates the OS keystore — but on Linux the keystore is `~/.claude/.credentials.json` itself (per [Claude Code authentication docs](https://code.claude.com/docs/en/authentication)). No keychain dance is needed. Lift the *patterns* (Stop-hook turn-boundary detection, 429-substring-match for reactive swap, signature-keyed hook install) but reimplement in ~500 lines of Python rather than fork ~8 kLOC of Go.
3. **Swap mechanism — file copy, not env-var.** AIMUX (and the user's existing `~/.claude-merkos/`) uses `CLAUDE_CONFIG_DIR` env-var swap. The user prefers in-place file copy so existing tmux/shell launches just work without wrapper or alias plumbing. Validated: on Linux, only two files are account-bound — `~/.claude/.credentials.json` (471 bytes, the full OAuth payload) and `~/.claude.json`'s `oauthAccount` block (and `userID` top-level field). Total swap footprint ≈ 37 KB, sub-millisecond.
4. **Progressive thresholds.** Per-account state machine: each account starts at `next_swap_at_pct = 50`. When active, on hitting threshold, daemon picks a swap target (lowest-usage other account, respecting priority). On returning to the same account later, threshold climbs: 50 → 75 → 90 → force. Yields natural load-balancing across the pool.
5. **Verified prerequisites** — `--dangerously-skip-permissions` does NOT bypass hooks ([permission-modes docs](https://code.claude.com/docs/en/permission-modes), confirmed by hook-guide writeups). `claude --resume <id>` works cross-`CLAUDE_CONFIG_DIR`; loads the conversation and waits for a new prompt (so a "Go continue." wake-up is required, as cux does).

**Out-of-band note: gym MCP disconnected.** The user requested AVC + gym integration. Only AVC MCP is connected this session — gym tools (gym_analyze, gym_consult, etc.) are unavailable. Plan written under AVC alone; gym-specific experiment-tracking deferred. See inbox entry "gym MCP disconnected during planning" for walk-back.

## Phases

Phases are labelled by priority, not strict dependency. Parallelism map below.

### Parallelism map

- **Phase 1.1 (account-file inventory)** is fully independent — read-only diff between `~/.claude.json` and `~/.claude-merkos/.claude.json`. Safe to run any time, including in parallel with continued design discussion.
- **Phase 1.2 (migration script)** depends on 1.1's findings — which keys to copy vs leave alone.
- **Phase 1.3 (manual swap CLI)** depends on 1.2 — needs the storage layout that migration produces.
- **Phase 2 (daemon)** depends on Phase 1 being stable. Within Phase 2, the polling loop, the threshold logic, and the SessionStart hook installer are three independent files — embarrassingly parallel.
- **Phase 3 (Tier 1 hot-swap)** depends on Phase 2's swap primitive + Phase 1's storage layout. The tmux-pane registry, the Stop hook installer, and the JSONL-tail watcher are independent within Phase 3.
- **Phase 4 (Tier 2)** and **Phase 5 (Tier 3)** depend on Phase 3's hot-swap primitive. Within each, independent files.
- **Phase 6 (operator controls)** is a config-layer change — depends only on Phase 2's config-loading infra. Could ship before or after Phases 3-5.

### Phase 1 — Foundations

Storage layout, migration, manual swap CLI. No daemon, no rotation. Establishes that swap-by-copy is reliable before any automation rides on it.

**1.1 — Account-file inventory.** Diff `~/.claude.json` against `~/.claude-merkos/.claude.json`. Confirm `oauthAccount` block + `userID` are the only account-bound top-level keys. Document the canonical "account-bound file set" in `docs/ARCHITECTURE.md`.

Demo: `python3 -c "import json; a=json.load(open('/home/rayi/.claude.json')); b=json.load(open('/home/rayi/.claude-merkos/.claude.json')); print([k for k in set(a)|set(b) if a.get(k)!=b.get(k)])"` outputs the differing keys, and they match what's documented.

**1.2 — Migration script.** `cus init` — discovers existing accounts on the machine (the live `~/.claude/` plus any sibling `~/.claude-*/` dirs), copies the relevant slice (`.credentials.json` + `.claude.json` — initially the whole file; surgical key-merge if 1.1 reveals significant non-account content) into `~/claude-accounts/<name>/`, writes `meta.yaml` per account (oauth email, priority, locked-sessions, etc.). Idempotent.

Demo: after `cus init`, `~/claude-accounts/{default,merkos}/` both exist with `credentials.json`, `claude.json`, `meta.yaml`. `~/.claude/.credentials.json` and `~/.claude.json` are untouched.

**1.3 — Manual swap CLI.** `cus switch <account-name>` — atomic two-file replacement using write-temp-then-rename. Saves current active state back into its source account dir first. Reports active account afterward.

Demo: `cus switch merkos` flips the active account; `claude --print "echo \$USER_ACCOUNT_EMAIL"` (or equivalent introspection) confirms; `cus switch default` flips back; previously-active session resumed via `claude --resume <id>` continues.

Effort estimate: 3-4 hours including testing.

### Phase 2 — Auto-rotation for new sessions

The daemon that makes this useful. Polls usage, decides swap target, flips active account at progressive thresholds. Does NOT touch live sessions.

**2.1 — `ccusage` integration.** `cus daemon` polls `ccusage --json` every N minutes (default 5). If `ccusage` is missing, prompt to install or fall back to Anthropic OAuth `/api/oauth/usage` direct (lift from `cux/internal/usage/usage.go:84-135`).

**2.2 — Per-account state machine.** `~/claude-accounts/state.json` (atomic JSON, tempfile+rename):
```yaml
accounts:
  default:
    current_5h_pct: 12.4
    current_7d_pct: 38.1
    next_swap_at_pct: 50   # climbs 50→75→90→force
    last_swap_ts: 2026-05-18T19:00:00Z
  merkos:
    ...
active: default
```

**2.3 — Decision loop.** On each poll: if `active.current_*_pct >= active.next_swap_at_pct`, pick swap target = lowest-usage other account (respecting priority from `config.yaml`), call swap primitive from Phase 1.3, bump the just-vacated account's `next_swap_at_pct` (50→75→75→90→90→force).

**2.4 — `SessionStart` hook.** Lightweight bash hook installed into `~/.claude/settings.json` via signature-keyed upsert: writes `<session-id>,<account-name>,<timestamp>,<TMUX_PANE>` to `~/claude-accounts/sessions.log` on session start. Provides visibility now and tmux-pane registry for Phase 3.

**2.5 — `cus status` CLI.** Reads `state.json`, pretty-prints current burn per account + which is active + next swap threshold per account. Optional: `--watch` redraws every N seconds.

**2.6 — `config.yaml` schema.**
```yaml
accounts:
  - name: default
    priority: 1
  - name: merkos
    priority: 2
poll_interval_seconds: 300
thresholds:
  steps: [50, 75, 90]      # post-90 = force
  five_hour: true          # apply to 5h window
  seven_day: true          # apply to 7d window
strategy: lowest_usage     # alt: round_robin, strict_priority
```

Demo: start `cus daemon` with two accounts. Burn account-A by running Claude work until it crosses 50%. Daemon flips active to account-B. Burn B to 50% — daemon flips back to A. Verify A's `next_swap_at_pct` is now 75 and B's is still 50.

**Stop here for v0.1.** Run on the user's real workload for several days. Validate state-machine behavior, polling reliability, swap atomicity. Layer Phase 3 only after this is stable.

Effort estimate: 4-6 hours.

### Phase 3 — Hot-swap of live sessions, Tier 1 (wait-for-Stop)

Adds: live sessions on the swapped-out account get cleanly relaunched on the new account when they next reach a turn boundary.

**3.1 — `Stop` hook installer.** Each `cus init` invocation installs a Stop hook that writes `<session-id>,<timestamp>` to `~/claude-accounts/stops.log`. Daemon tails this.

**3.2 — Tmux-pane registry.** Phase 2.4's SessionStart hook already captures `TMUX_PANE`. Daemon reads `sessions.log` + active-session info to know which pane hosts which session.

**3.3 — Hot-swap orchestrator.** When daemon decides to swap and live sessions exist on the affected account:
- Wait for next Stop signal for each session.
- For each: `tmux send-keys -t <pane> -l "/exit"; sleep 0.3; tmux send-keys -t <pane> C-m`.
- After Claude exits, swap account files.
- Relaunch in same pane: `tmux send-keys -t <pane> -l "claude --resume <id> 'Go continue.'"; sleep 0.3; tmux send-keys -t <pane> C-m`.

**3.4 — Cache-bust window optimization.** Before initiating swap, check session JSONL last-message timestamp. If < 5 min old, defer swap (cache still warm — swapping now incurs extra burn rebuilding cache on new account). If > 5 min, cache is cold anyway — free swap.

Demo: live session on account-A crosses 50%. Daemon waits for Stop, sends `/exit` via tmux, swaps to account-B, relaunches with `--resume`. Conversation continues from where it left off. New session log line written to `sessions.log` showing new account.

Effort estimate: 4-6 hours.

### Phase 4 — Tier 2 (graceful pause-message injection)

For sessions that don't naturally hit Stop quickly (long autonomous loops, claude spinning hard on a thought).

**4.1 — Threshold-aware aggressiveness.** If `next_swap_at_pct = 75` and session is mid-turn (no Stop in N seconds):
- Inject "please pause your current thought — we're swapping accounts, you'll resume on the other side" via `tmux send-keys` user prompt.
- Claude reads as user message, wraps up gracefully, hits Stop.
- Swap proceeds as Phase 3.

**4.2 — Mid-turn detection.** JSONL tail: if no new line in the active session's transcript for N seconds, "idle." If new lines arriving but no Stop, "mid-turn." Threshold N configurable; default 30s.

Demo: autonomous loop session at 75%. Daemon injects pause prompt. Claude responds with wrap-up + hits Stop. Swap completes.

Effort estimate: 3-4 hours.

### Phase 5 — Tier 3 (force interrupt + 429 reactive)

Hard-cap protection. When usage hits 90% or we've already received a 429 from the API.

**5.1 — `PostToolUseFailure` hook.** Substring-match `rate_limit | usage limit | overloaded_error` in error body. Writes to `~/claude-accounts/429.log`. Daemon reacts immediately without waiting for next poll.

**5.2 — Force-interrupt sequence.** If `next_swap_at_pct = force` (90+) or 429 received:
- `tmux send-keys -t <pane> Escape` to cancel running tool.
- Post inbox entry to `~/claude-accounts/inbox.md` listing any shells the session had running, for user review.
- Force-exit + swap + relaunch as Phase 3.

**5.3 — Subagent / shell skip-guard.** Before force-interrupt, check for active subagents (read `~/.claude/projects/.../<id>.jsonl` last few lines, look for Task tool calls without matching completions). If present and `next_swap_at_pct = 75` (not force), defer swap and retry next Stop. If `force`, proceed with kill-warning logged to inbox.

Demo: account hits 90% during a Bash-heavy session. Daemon interrupts, logs shell context to inbox, swaps, relaunches. User reviews inbox and decides whether to re-kick the killed work on the new account.

Effort estimate: 4-5 hours.

### Phase 6 — Operator controls (can ship parallel to 3-5)

**6.1 — Account priority levels.** Already in `config.yaml`. Daemon's swap-target picker respects priority order ("use account-1 first, account-2 as fallback, account-3 only if both are at 90%").

**6.2 — Lock-session-to-account.** `cus pin <pane-id-or-session-id> <account-name>` marks a session as un-swappable. Daemon skips it during swap decisions. Stored in `state.json` under `locks`.

**6.3 — Whitelist patterns.** `config.yaml` entries like `never_restart_patterns: ["babysitter:*", "long-running-experiment-*"]` matched against tmux pane name or session-start commit message. Daemon skips matching panes.

**6.4 — Statusline integration.** Optional Claude Code statusline script that reads `state.json` and surfaces active account + headroom in the TUI.

Effort estimate: 2-3 hours.

## Explicitly out of scope

- **macOS Keychain handling.** This tool is Linux-only for v1. macOS users have `cux` and `AIMUX`. Multi-OS support is a separate plan.
- **Windows.** Same reasoning.
- **Concurrent multi-account on one machine.** Architecturally precluded by the in-place file-swap design — one account active at a time across all sessions. If you want different sessions on different accounts simultaneously, use AIMUX's `CLAUDE_CONFIG_DIR` pattern instead.
- **API-key/Bedrock/Vertex auth.** Only OAuth (Pro/Max subscription) accounts. If a user has API-key auth, they pay per token and rotation logic is different.
- **Anthropic SaaS billing-aware swap.** We swap on usage % only. Spend caps in dollars are not in scope; if Anthropic ships a real-time spend API, revisit.
- **Cross-machine coordination.** Each machine runs its own daemon with its own `state.json`. No shared lock to prevent two machines burning the same account in parallel. If this becomes a problem, a shared-state mode (sqlite over NFS, or a tiny server) can be a future plan.
- **A GUI.** TUI-only. Statusline integration (Phase 6.4) is the closest we get.
- **Auto-account-creation / OAuth flow scripting.** Adding a new account is a manual `claude /login` followed by `cus init` discovery. We do not script the OAuth login flow.

## Open questions that could change the plan

**1. Should `cus init` migrate `~/.claude-merkos/` automatically, or require explicit `cus import-from-dir ~/.claude-merkos/`?**
Leaning: auto-detect any sibling `~/.claude-*/` dirs and offer to import. Less friction. User decides at the prompt rather than running a separate command. Decision criterion: does the user want one-shot setup, or explicit per-account ceremony?

**2. Surgical key-merge or whole-file swap for `~/.claude.json`?**
Leaning: whole-file swap for v0.1 (simpler, atomic). If Phase 1.1's diff shows lots of non-account state in `.claude.json` that needs to persist across swaps, escalate to surgical key-merge (jq-style — only swap `oauthAccount` + `userID`). Decision criterion: how many `~/.claude.json` keys are session-local vs account-local?

**3. Daemon supervision — systemd, tmux, or in-process?**
Leaning: support both. Ship a `cus daemon --foreground` for tmux-pane hosting and a `systemd/cus.service` unit file for users who want background. Decision criterion: user preference; both are <20 lines.

**4. Cross-account `--resume` — does it Just Work, or are there edge cases?**
Believed safe based on research. Open until Phase 3 actually tries it. If it doesn't work, fallback is to symlink `~/claude-accounts/*/projects` → `~/claude-accounts/shared/projects/` so all accounts see all transcripts. Decision criterion: empirical Phase 3 testing.

**5. v0.1 scope — Phase 1+2 only, or include Phase 3 Tier 1?**
Strong leaning: Phase 1+2 only. New-session auto-rotation is 80% of the value; hot-swap is more surface to debug. Validate Phase 1+2 for several days before layering on. Decision criterion: user's appetite for complexity in v0.1.

**6. Gym MCP integration once reconnected.**
The user wanted AVC + gym. Gym disconnected this session. When reconnected, what gym features fit this tool? Possibilities: `gym_design` for design-doc capture (already covered by AVC plan_template); `gym_drill` for stress-testing the swap primitive; `gym_audit_coding` for code review. Decision criterion: which gym tools complement AVC for an engineering project (vs research project).

**7. Distribution.**
Leaning: single-file Python script (`cus.py`) with PEP 723 inline metadata so `uv run cus.py daemon` Just Works. Stdlib + `ccusage` (Node) + `tmux` are the only external dependencies. Easy to `scp` across the user's N machines. Decision criterion: user's `uv` adoption.

---

## Status

**Phase 0 — DONE this session:** existing-tool survey, architecture decisions, swap-mechanism validation, this plan.
**Phase 1 — NEXT:** awaiting user confirmation of v0.1 scope (open question 5). Once confirmed, Phase 1.1 (inventory) takes 5 minutes and de-risks 1.2/1.3.
