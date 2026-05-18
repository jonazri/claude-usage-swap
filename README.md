# claude-usage-swap

Auto-rotate Claude Code OAuth accounts based on usage thresholds.

Single-file Python tool that watches `ccusage --json` and swaps the active OAuth identity (the 471-byte `.credentials.json` + two keys in `~/.claude.json`) when accounts approach their 5-hour or weekly cap. Per-account progressive thresholds (50% → 75% → 90% → force) yield natural load-balancing across an N-account pool.

**Status: planning + Phase 1 in progress.** See [`docs/plans/2026-05-18-claude-usage-swap.md`](docs/plans/2026-05-18-claude-usage-swap.md) for the build plan.

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
