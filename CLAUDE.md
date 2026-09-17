# CLAUDE.md — working in `claude-usage-swap` (`cus`)

Guidance for AI coding agents (and humans) working in this repo. Start with
[`README.md`](README.md) for what `cus` is and [`SECURITY.md`](SECURITY.md) for
the safety model — **read SECURITY.md before touching anything that reads, moves,
or writes credentials.** [`CONTRIBUTING.md`](CONTRIBUTING.md) covers PR mechanics.

## What this is, in one paragraph

`cus` auto-rotates Claude Code OAuth accounts based on usage so a long-running
setup doesn't stall at a 5-hour or weekly cap. It's a **single-file Python CLI +
`systemd --user` daemon** (`cus.py`), Linux-only, that reads Claude Code's
credential files and swaps the *active* credentials between accounts atomically,
optionally hot-resuming in-flight `tmux` sessions via `claude --resume`. It leans
on two **undocumented** Anthropic surfaces — the `CLAUDE_CONFIG_DIR` env var and
the OAuth usage endpoint — either of which can change upstream at any time
(SECURITY.md spells out the risk).

## Architecture at a glance

- **`cus.py`** — everything: the CLI subcommands, the daemon loop, credential
  I/O, usage polling, and the swap state machine. It's large and single-file by
  design; use `grep`/symbol search to navigate rather than reading top-to-bottom.
- **`cus.service`** (`systemd --user`; installed on the host, not committed in-repo) — the background daemon that polls usage
  and moves credentials. **The config file is hot-read; the *code* is not** —
  after editing `cus.py` you must `systemctl --user restart cus.service` for the
  daemon to pick it up. Config changes need no restart.
- **State & config** live outside the repo (a YAML config + a JSON state file in
  the user's config area). Never hardcode a user's paths, account names, or
  machine specifics into the code or docs — keep those in the operator's own
  config, not in the repo.
- **Core concepts** (see README for the full model):
  - *Account* — one Claude Pro/Max login.
  - *Shared mount* — the plain `~/.claude` credential dir a bare `claude` uses;
    the daemon swaps which account it points at.
  - *Slots / lanes* — per-session credential dirs (via `CLAUDE_CONFIG_DIR`) so
    individual sessions can be pinned to specific accounts independent of the mount.
  - *Login families* — independent credential sets for one account, so two lanes
    on the same account don't log each other out. Pool exhaustion is a real
    failure mode; a launch into a dedicated slot can be refused when no free,
    independent family is available.
  - *Pools* (`premium` / `standard`) — gate whether a lane may draw the per-model
    weekly (e.g. "Fable") allowance.
  - *Locked slots* — the daemon will not rotate, garbage-collect, or let another
    session share a locked slot. This is the real "freeze" mechanism.

## Editing rules that matter here

- **Credential safety is the prime directive.** Code paths that copy, install, or
  save back credentials must never clobber a *live* mount with stale or
  wrong-identity tokens. When in doubt, refuse and surface a clear error rather
  than overwrite. Assume every credential write can log a user out of a working
  session if it's wrong.
- **Never log, print, or commit token material** (access/refresh tokens, cookies).
  Redact identifiers in audit logs. Tests and fixtures use fakes, never real creds.
- **Backward-compatible by default.** Config-key, state-schema, CLI-flag, and
  on-disk-format changes should keep existing setups working. Add with sensible
  defaults; gate a breaking change behind explicit opt-in and call it out in the PR.
- **Undocumented-surface fragility.** Anything touching `CLAUDE_CONFIG_DIR`
  semantics or the usage endpoint's response shape is load-bearing and brittle —
  add a focused test and a comment explaining the assumption when you change it.
- **Comment the *why*.** This codebase encodes many hard-won incident fixes;
  when you add or change one, say what failure it prevents and when it was found,
  so the next reader doesn't "simplify" it back into the bug.

## Testing

- Tests live in [`tests/`](tests/) as `pytest`-style modules (`tests/test_*.py`),
  and CI runs them on every push (`.github/workflows/ci.yml`).
- Run the suite before opening a PR: `python -m pytest tests/` (or `pytest`).
- New behavior — especially around swaps, polling, reset/rollover math,
  credential save-back, and disabled/rate-limited accounts — should ship with a
  test. Many existing tests are named after the incident they lock in; follow
  that convention.

## Operating a long-running fleet (context for the ops-facing pieces)

Two documents describe how `cus` is meant to be *run*, not just built:

- [`skills/watch.md`](skills/watch.md) — the **watchdog** pattern: a long-running
  monitoring session that checks fleet health each interval (memory/OOM, daemon
  liveness, per-account usage trends, stuck/logged-out sessions) and acts only on
  genuine problems. It documents the standing check loop and how to migrate the
  watchdog between accounts safely.
- [`docs/RESUME.md`](docs/RESUME.md) — the **crash/reboot recovery runbook**:
  what survives a reboot (transcripts, pushed work, cus state) and what does not
  (session-local timers), plus the step-by-step order to bring a fleet back.

Operational principles worth internalizing before acting on a live fleet:

- **Act on real signals, not guesses.** Move or swap a lane on an actual block,
  logout, or wall — not on a low token-TTL, a stale cached number, or a schedule.
  The shared mount self-heals its own tokens; don't proactively overwrite it.
- **Don't churn working lanes.** Every swap busts the prompt cache and risks a
  credential race. Prefer the least-disruptive fix.
- **Prefer in-place account re-homing** (`cus slot move`) and **locking** over
  tearing down and relaunching a live session.
- **A stale cached usage number is not a current one.** Values the tool marks as
  unreconfirmed (e.g. a trailing `~`) must not drive a swap decision.

## Autonomy posture

Code changes go through **feature branches and PRs** (see `CONTRIBUTING.md`);
risky changes to credential or swap logic deserve extra review and tests. Live
operational actions (swaps, mount moves, locks) are reversible and can be done
decisively, but they touch a running system — verify the target before acting and
prefer the smallest change that fixes the real problem.

## Deploy after changes (this fork runs the live daemon)

- Treat every configuration change and every merge as incomplete until the live daemon has been deployed.
- Deploy by restarting the systemd-managed daemon: `systemctl --user restart cus.service`.
- Do not start a separate foreground `cus daemon`; it would compete with the systemd service.
- Before reporting completion, verify that `cus.service` is active with a new start time/PID, the daemon log contains a completed post-restart cycle, and `cus sos` reports no urgent condition.
- If deployment or verification cannot be completed, report the work as **deployment pending** rather than complete.
