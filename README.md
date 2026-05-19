# claude-usage-swap

Auto-rotate Claude Code OAuth accounts based on usage thresholds.

Single-file Python tool that watches `ccusage --json` and swaps the active OAuth identity (the 471-byte `.credentials.json` + two keys in `~/.claude.json`) when accounts approach their 5-hour or weekly cap. Per-account progressive thresholds (50% → 75% → 90% → force) yield natural load-balancing across an N-account pool.

**Status: all 6 phases shipped (v0.1).** See [`docs/plans/2026-05-18-claude-usage-swap.md`](docs/plans/2026-05-18-claude-usage-swap.md) for the build plan.

## Storage layout (post-migration)

Each account lives in its own `CLAUDE_CONFIG_DIR`-compatible dir under `~/claude-accounts/`:

```
~/claude-accounts/
  config.yaml                     # editable: thresholds, strategy, hot_swap, locks
  state.json                      # runtime: active, per-account usage, swap history
  SOS.md                          # written by daemon if human action needed
  daemon.log                      # daemon stdout/stderr
  inbox.md                        # autonomous decisions worth user review
  account-default/                # a full CLAUDE_CONFIG_DIR
    .credentials.json             # OAuth tokens, 0600
    .claude.json                  # account-bound state (userID, oauthAccount, etc.)
    meta.yaml                     # priority, locked_sessions, oauth_email, ts
    projects/ → ~/.claude/projects/    # symlink — shared history across accounts
    plugins/  → ~/.claude/plugins/     # symlink
    agents/, skills/, commands/, memory/, hooks/, scripts/  # symlinks if present
  account-merkos/
    ...
  account-<your-new-one>/
    ...
```

The live `~/.claude/` is the "currently active" mount point that Claude Code reads from when no `CLAUDE_CONFIG_DIR` is set. Swap = copy `.credentials.json` + account-bound keys from `account-<target>/` into `~/.claude/`. No more ad-hoc `~/.claude-<name>/` dirs going forward.

## Adding accounts

```bash
# Add a new account: creates ~/claude-accounts/account-<name>/ and prints
# the exact login command.
python3 cus.py add work

# Output:
#   Created /home/rayi/claude-accounts/account-work
#   To log in to this account, run:
#     CLAUDE_CONFIG_DIR=/home/rayi/claude-accounts/account-work/ claude
#   After logging in, register it:
#     python3 ~/repos/claude-usage-swap/cus.py init --force && cus poll

# Re-login an existing account (e.g. when SOS says tokens expired):
python3 cus.py relogin merkos

# Or use --exec to immediately launch claude under that dir:
python3 cus.py add work --exec
python3 cus.py relogin merkos --exec
```

## Usage

```bash
# Phase 1 — discover existing config dirs, import as accounts
python3 cus.py init --dry-run             # preview
python3 cus.py init                       # commit

# Inspect
python3 cus.py list                       # accounts + OAuth identities
python3 cus.py status                     # active + usage + locks + live sessions
python3 cus.py config                     # effective config (defaults merged)
python3 cus.py statusline                 # one-line summary (for CC statusLine)

# Manual swap
python3 cus.py switch merkos --dry-run    # preview the plan
python3 cus.py switch merkos              # commit

# Phase 2 — auto-rotation daemon
python3 cus.py poll                       # one-shot usage poll via OAuth API
python3 cus.py daemon --once              # single poll-decide-act cycle
python3 cus.py daemon --once --no-execute # decide but don't actually swap
python3 cus.py daemon                     # run forever (foreground)

# Phase 2 — hook installation (signature-keyed, won't clobber other tools)
python3 cus.py hooks list                 # current state
python3 cus.py hooks install              # install enabled hooks
python3 cus.py hooks uninstall            # remove cus entries

# Phase 6 — operator controls
python3 cus.py pin %12 default            # pin tmux pane %12 to default account
python3 cus.py unpin %12                  # remove pin
python3 cus.py init-systemd --enable      # systemd --user unit + start
```

## SOS — when human action is needed

`cus sos` (or just run the daemon — it does the same checks) surfaces conditions requiring you to step in:

- OAuth token expired on any account — concrete `CLAUDE_CONFIG_DIR=... claude` re-login command provided.
- All accounts blocked (token expired / rate limited / poll error) — daemon can't proceed.
- Active account over threshold AND no swap target available.
- Stale usage data (no fresh poll in >4 cycles) — daemon may be down.
- Daemon pid recorded but process gone — needs restart.

Channels:

1. **Claude Code statusLine** — shows `🚨 cus SOS: <summary>` when conditions exist. Wired into `~/.claude/settings.json` automatically by `cus init` if no existing statusLine is present.
2. **`cus sos` CLI** — exit code 1 + printed actions when conditions exist; exit 0 + "All clear" otherwise.
3. **`~/claude-accounts/SOS.md`** — written by the daemon (and by `cus sos`) when conditions exist; deleted when clear. Cat this file anytime to see what's needed.
4. **Desktop notification** via `notify-send` when conditions *change* (no spam — only fires on signature changes).

## Phases (all shipped)

1. **Foundations** — `cus init/list/status/switch`. Surgical 2-file swap (`.credentials.json` whole-file + `userID`+`oauthAccount` keys in `.claude.json`).
2. **Auto-rotation daemon** — `cus daemon` polls OAuth usage API per-account, progressive thresholds (50→75→90→force), strategy picker (lowest_usage / drain / strict_priority / round_robin), `SessionStart`/`Stop`/`PostToolUseFailure`/`PreToolUse`/`SubagentStop` hooks.
3. **Hot-swap Tier 1** (wait-for-Stop) — enabled by `hot_swap.enabled: true`. Live sessions paused at next turn boundary, swap, relaunched with `--resume`.
4. **Tier 2** (pause-message injection) — at `tier_2_at_pct` (default 75), daemon injects a pause-message into the running TUI via `tmux send-keys` and waits for the resulting Stop.
5. **Tier 3** (force interrupt + 429 reactive) — at `tier_3_at_pct` (default 90), Escape sent via tmux to interrupt running tools; shell context logged to `~/claude-accounts/inbox.md`. `PostToolUseFailure` hook detects 429 substring-match for immediate reactive swap.
6. **Operator controls** — `cus pin/unpin` for per-pane lock, `never_restart_patterns` for whitelist, `cus statusline` for Claude Code statusLine integration, `cus init-systemd` for `systemctl --user` setup.

## Config (`~/claude-accounts/config.yaml`)

Generated by `cus init`. Everything overridable; defaults documented in `cus.py:DEFAULT_CONFIG`. Run `python3 cus.py config` to dump the effective merged config.

Key knobs:
- `poll_interval_seconds` (default 300)
- `strategy: lowest_usage | drain | strict_priority | round_robin`
- `thresholds.steps: [50, 75, 90]` — progressive per-account ladder
- `thresholds.reset_below_pct: 50` — reset ladder when both windows below this
- `hot_swap.enabled: false` — Phase 3+ opt-in
- `hot_swap.{tier_2_at_pct, tier_3_at_pct, pause_message, wake_up_message}`
- `hot_swap.cache_bust_window_seconds: 300` — defer Tier 1 if last msg < this old
- `subagent_skip.{enabled, defer_below_tier}`
- `reactive.enabled: true` — 429 reactive swap
- `session_locks.{pinned, never_restart_patterns}`

## System requirements

Python 3.11+, `click`, `pyyaml`, `tmux` (only needed for Phase 3+ hot-swap of tmux'd sessions). The shebang uses `uv run --script` with PEP 723 inline metadata so `uv run cus.py` resolves deps automatically; or just `python3 cus.py` if click+pyyaml are system-wide.

## Walk-back

Everything is reversible. See `inbox.md` for the load-bearing autonomous decisions with their concrete walk-back paths.

- Manual swap: `cus switch <previous-name>` restores prior state.
- Daemon-driven swap: same — every entry in `swap_history` (in `state.json`) is reversible by swapping back.
- Hooks: `cus hooks uninstall` removes our entries from `~/.claude/settings.json`. Other tools' entries are untouched (signature-keyed).
- Storage: `~/claude-accounts/` is purely additive; deleting it removes nothing from `~/.claude/`.
- systemd unit: `systemctl --user disable cus.service` + delete the unit file.



## Why

Manual workaround for Claude Code's 5h / weekly cap is friction: `/exit` the session, change `CLAUDE_CONFIG_DIR`, relaunch with `--resume`. Doing it across many concurrent sessions and many machines is even worse. This tool automates the rotation.

Existing tools solve adjacent problems but not this exact one:
- [cux](https://github.com/inulute/cux) — auto-rotation, but in-place credential keystore swap with single global threshold. Overengineered for Linux (no OS keystore involvement on Linux).
- [AIMUX](https://github.com/Digital-Threads/aimux) — multi-account profile manager, but manual swap only.
- [teamclaude](https://github.com/KarpelesLab/teamclaude), [ccflare](https://github.com/snipeship/claude-balancer) — proxy-based architectures, different shape.

This tool fills the gap: file-copy swap (not env-var, not keystore) + progressive per-account thresholds + tmux-aware hot-swap (Phase 3+).

## Quick reference

| File | What |
|---|---|
| [`docs/plans/2026-05-18-claude-usage-swap.md`](docs/plans/2026-05-18-claude-usage-swap.md) | Build plan: 6 phases, parallelism map, open questions |
| [`docs/journal/2026-05-18-claude-usage-swap-planning.md`](docs/journal/2026-05-18-claude-usage-swap-planning.md) | Planning session narrative |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Verified file layout, swap algorithm, state machine |
| [`inbox.md`](inbox.md) | Agent decisions made autonomously during planning |

## Scope (v1)

- Linux only. macOS/Windows out of scope.
- OAuth (Pro/Max) accounts only. API-key/Bedrock/Vertex out of scope.
- One active account at a time per machine (in-place file swap precludes concurrent multi-account; use AIMUX for that).
- Per-machine state (no cross-machine coordination).
