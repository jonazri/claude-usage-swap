# claude-usage-swap (`cus`)

Auto-rotate Claude Code OAuth accounts based on usage thresholds. Single-file Python, Linux-only, ~2000 LOC.

When one of your Claude Pro/Max accounts approaches its 5-hour or weekly cap, `cus` swaps the active credentials to a different account — atomically, with optional hot-swap of in-flight tmux sessions so conversations preserve via `claude --resume`.

> **Status:** v0.1, ready to use. Production-tested on the author's setup. No external maintainers; PRs welcome.

## What problem this solves

Claude Code's 5-hour and weekly caps are per-account. If you have multiple accounts (work + personal, or multiple plans), the manual workaround is:

1. `/exit` your sessions
2. `export CLAUDE_CONFIG_DIR=~/.claude-account2/`
3. Relaunch with `claude --resume <id>`

Across many concurrent sessions and many machines, this gets old. `cus` automates it:

- Polls each account's usage via the same Anthropic OAuth endpoint Claude Code itself uses for `/usage`
- Per-account progressive thresholds (50% → 75% → 90% → force) yield natural load-balancing across an N-account pool
- Atomic two-file swap (`.credentials.json` + a couple of keys in `~/.claude.json`) — no env-var threading
- Optional hot-swap of live sessions in tmux panes (pause-message injection → `/exit` → `claude --resume <id> "Continue."`)
- SOS subsystem surfaces conditions requiring human action via `cus sos` CLI, `~/claude-accounts/SOS.md`, Claude Code statusLine, and `notify-send`

## Existing tools, and why this one

`cus` exists because no single tool does *all* of: per-account progressive thresholds + tmux-aware hot-swap + `CLAUDE_CONFIG_DIR`-style file swap + N-account pool. Closest competitors:

- [cux](https://github.com/inulute/cux) — Go wrapper, in-place credential keystore swap (single global threshold). Methodology lifted; reimplemented in Python without the keystore manipulation (unnecessary on Linux).
- [AIMUX](https://github.com/Digital-Threads/aimux) — manual `CLAUDE_CONFIG_DIR` profile switcher; no auto-rotation.
- [teamclaude](https://github.com/KarpelesLab/teamclaude), [ccflare](https://github.com/snipeship/claude-balancer) — proxy-based, different architecture.
- [ccusage](https://ccusage.com) — usage display only; doesn't switch.

## Installation

### Requirements

- Linux (macOS / Windows out of scope for v1 — see [Architecture](docs/ARCHITECTURE.md))
- Python 3.11+
- `click` and `pyyaml` (system-wide or via `uv`)
- `tmux` (only needed for hot-swap of live sessions)
- One or more Claude Pro/Max accounts

### Steps

```bash
# 1. Clone
git clone https://github.com/rayistern/claude-usage-swap ~/repos/claude-usage-swap
cd ~/repos/claude-usage-swap

# 2. Discover existing Claude config dirs, migrate to managed layout
python3 cus.py init

# 3. Verify
python3 cus.py list             # accounts found + their OAuth identities
python3 cus.py poll             # one-shot usage poll
python3 cus.py status           # current state

# 4. Install Claude Code hooks (lifecycle events the daemon needs)
python3 cus.py hooks install

# 5. Run as a systemd --user service (survives reboot)
python3 cus.py init-systemd --enable

# 6. (Optional) Wire the statusline into Claude Code
# Edit ~/.claude/settings.json and add:
# "statusLine": {"type": "command", "command": "python3 /full/path/to/cus.py statusline"}
```

## Quick reference

```bash
cus init                      # discover + migrate accounts (idempotent)
cus init --force              # refresh stale credential snapshots
cus list                      # accounts + OAuth identities
cus status                    # active + per-account state + live sessions
cus sos                       # exit 1 + actions if anything needs you
cus statusline                # one-line summary (for CC statusLine)
cus config                    # effective merged config

cus poll                      # one-shot usage poll
cus daemon                    # foreground loop (or use systemd)
cus daemon --once             # single cycle and exit
cus daemon --once --no-execute # dry-run (decide, don't swap)

cus switch <name>             # manual atomic swap
cus switch <name> --dry-run   # preview

cus add <name>                # create a new account dir
cus add <name> --exec         # ... and launch claude under it
cus relogin <name>            # print login command for token-expired account
cus relogin <name> --exec     # ... and launch claude under it

cus pin <pane> <account>      # pin a tmux pane to an account (never swap)
cus unpin <pane>              # remove pin

cus hooks install/uninstall/list
cus init-systemd --enable     # install + start systemd --user service
```

## How a swap actually happens

Three levels of aggressiveness, controlled by `config.yaml`:

### Level 3: auto-swap for new sessions (default; `hot_swap.enabled: false`)

When an account crosses its threshold step, the daemon:
1. Copies `~/.claude/.credentials.json` and account-bound keys of `~/.claude.json` into `~/claude-accounts/account-<current>/` (preserves any token refresh)
2. Copies the same files *from* `~/claude-accounts/account-<target>/` *into* the live `~/.claude/` location
3. Updates `state.json` with swap history

In-flight Claude sessions keep using whatever creds they loaded at start. New `claude` invocations after the swap use the new account.

### Level 4: hot-swap of live sessions (`hot_swap.enabled: true`)

Adds tier-graded behavior. When threshold is crossed:

- **Tier 1** (first step, default 50%): wait for the session's next `Stop` hook (turn boundary). Defer if cache is still warm (last message < 5 min ago — swap would burn cache rebuild).
- **Tier 2** (default 75%): inject "please pause, we're swapping accounts" via `tmux send-keys` to make Claude wrap up gracefully. Then proceed as Tier 1.
- **Tier 3** (default 90%): send `Escape` to interrupt running tools. Log shell context to `~/claude-accounts/inbox.md` for your review. Then swap.

After the swap, the daemon:
1. Sends `/exit` to the tmux pane (via `tmux send-keys`)
2. Relaunches in the same pane with `cd <cwd> && claude --resume <session-id> "Continue."`
3. The conversation continues from where it left off, now under the new account

### Reactive swap (any level)

The `PostToolUseFailure` hook substring-matches `rate_limit | usage limit | overloaded_error` in tool error bodies. On a hit, the daemon swaps immediately without waiting for the next poll.

## Progressive thresholds (the novel bit)

Each account has its own `next_swap_at_pct` field. Starts at 50; climbs through `[75, 90, force]` each time we swap *out* of it. Reset to 50 when both windows drop below `reset_below_pct`.

Result: account A swapped out at 50%; we drain B until 50%; back to A but it's still at ~50%, so it doesn't trip again until 75%; etc. Naturally load-balances across the pool instead of hammering A → swap → A → swap.

## SOS — when human action is needed

The daemon can't fix everything autonomously. When tokens expire, all accounts hit cap, or the daemon itself crashes, it surfaces an SOS through four channels:

- **`cus sos` CLI** — exit 1 + printed actions; exit 0 = all clear
- **`~/claude-accounts/SOS.md`** — written every cycle when conditions exist, deleted when clear
- **Claude Code statusLine** — shows `🚨 cus SOS: <reason>` instead of the normal `cus:<account>...`
- **`notify-send` desktop notification** — fires on signature changes (no spam)

Conditions checked:
- OAuth token expired on any account → `cus relogin <name>`
- All accounts blocked → re-login or wait
- Active over threshold with no swap target → add another account
- Stale poll (>4 cycles missed) → daemon may be down
- Daemon pid recorded but process gone → restart

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for the full catalog with recipes.

## Storage layout

```
~/claude-accounts/
  config.yaml                     # operator config (edit this)
  state.json                      # runtime state (don't edit while daemon running)
  SOS.md                          # current SOS conditions (or absent if clear)
  inbox.md                        # autonomous decisions (Tier 3 force-interrupts)
  daemon.log                      # daemon stdout/stderr
  account-default/                # a full CLAUDE_CONFIG_DIR
    .credentials.json
    .claude.json
    meta.yaml
    projects/ → ~/.claude/projects/   # symlinks for shared state
    plugins/, agents/, skills/, commands/, memory/, hooks/, scripts/ (symlinks)
  account-merkos/
    ...
```

Each `account-<name>/` directory is a fully-functional `CLAUDE_CONFIG_DIR`. To log into a new account: `CLAUDE_CONFIG_DIR=~/claude-accounts/account-<name>/ claude` (or use `cus add <name>` to create + launch in one step).

The live `~/.claude/` is the "currently active" mount point that Claude Code reads from when no env var is set. Swap = copy from `account-<target>/` into `~/.claude/`.

## Config reference

`~/claude-accounts/config.yaml`:

```yaml
accounts:
  - name: default
    priority: 1
  - name: merkos
    priority: 1

poll_interval_seconds: 300       # daemon polls every N seconds

strategy: lowest_usage           # lowest_usage | drain | strict_priority | round_robin

thresholds:
  steps: [50, 75, 90]            # progressive ladder
  five_hour: true
  seven_day: true
  reset_below_pct: 50

hot_swap:
  enabled: true                  # false = level 3 (new sessions only)
  tier_2_at_pct: 75
  tier_3_at_pct: 90
  pause_message: "please pause your current thought — we're swapping accounts..."
  wake_up_message: "Continue where you left off."
  cache_bust_window_seconds: 300
  mid_turn_idle_seconds: 30
  stop_wait_timeout_seconds: 300
  pause_response_timeout_seconds: 120

subagent_skip:
  enabled: true
  defer_below_tier: 3            # tier_3 force proceeds regardless

reactive:
  enabled: true                  # detect 429s via PostToolUseFailure hook

session_locks:
  pinned: {}                     # {pane_or_session_id: account_name}
  never_restart_patterns: []     # regex list

hooks:
  install_session_start: true
  install_stop: true
  install_post_tool_use_failure: true
  install_pre_tool_use: true
  install_subagent_stop: true
```

Run `cus config` to see the effective merged config.

## Documentation

- [docs/RUNBOOK.md](docs/RUNBOOK.md) — day-to-day operations
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — SOS catalog + common issues
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — file layout, swap algorithm, state machine
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — PR / development guide
- [docs/plans/](docs/plans/) — original build plan (AVC methodology)
- [docs/journal/](docs/journal/) — session-scoped writeups
- [inbox.md](inbox.md) — load-bearing autonomous decisions with walk-back paths

## Walk-back / uninstall

Everything is reversible:

```bash
systemctl --user disable --now cus.service
cus hooks uninstall                            # removes our hook entries from settings.json
# Manually remove "statusLine" key from ~/.claude/settings.json (or restore from .bak)
mv ~/claude-accounts ~/claude-accounts.removed # purely additive; doesn't touch ~/.claude/
```

Conversation history, plugins, agents, etc. live in `~/.claude/` and are never modified by `cus`.

## Limitations

- **Linux only.** macOS Keychain handling is non-trivial; out of scope for v1.
- **OAuth (Pro/Max) accounts only.** API-key / Bedrock / Vertex auth not supported.
- **One active account at a time per machine.** The file-copy swap model precludes concurrent multi-account on one machine. Use AIMUX if you need that.
- **Cross-machine coordination not implemented.** If you run `cus daemon` on multiple machines, they don't coordinate — two daemons could pick the same swap target.
- **No SessionEnd hook in Claude Code** — session liveness is inferred from JSONL transcript mtime + Stop hook recency. Long-idle sessions may be wrongly marked dead.

## License

MIT (see [LICENSE](LICENSE)).

## Credits

Methodology lifted from [cux](https://github.com/inulute/cux). Anthropic OAuth usage endpoint contract verified live against the same API Claude Code uses for `/usage`.
