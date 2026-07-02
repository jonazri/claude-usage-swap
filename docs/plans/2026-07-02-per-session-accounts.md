# Plan — per-session account slots (`mode: per_session`) (2026-07-02)

> **Built 2026-07-02 (same day, this branch).** All four phases implemented on `feature/per-session-accounts-20260702`: Phase 1 (inventory table in ARCHITECTURE.md; `cus slot create/list/gc`, `cus doctor --fix-dirs`, `cus sync-config`), Phase 2 (`execute_swap(slot=...)` mount parameterization incl. journal/crash-recovery; `cus launch`; env-based slot/account detection in statusline + SessionStart hook), Phase 3 (per-slot decision loop with fan-out, per-slot reactive 429 attribution, occupied/idle poll cadence, bare-launch observe-only, periodic guarded save-back + slot gc), Phase 4 (`cus mode`, per_session SOS conditions, docs). Deviations from this plan are noted inline in code comments; the notable ones: launch auto-pick shims `pick_swap_target` with a sentinel active rather than a new scorer; reactive 429 attribution got its own per-session function instead of reusing `check_rate_limit_reactive`; slot gc got a 72h idle grace (`per_session.slot_gc_idle_hours`) because eagerly reaping free slots defeats launch reuse. Production remains in `mode: global` — entering per_session is an operator step (`cus mode per-session`), including the first `cus doctor --fix-dirs` run against the live tree.

> **Revision 2 (2026-07-02, same day).** v1 of this plan launched sessions directly inside account dirs and moved a session between accounts by relaunching it (`/exit` → new `CLAUDE_CONFIG_DIR` → `claude --resume`). User review caught that this loses the property they most value about today's global swap: **live sessions never restart** — the in-place file swap happens underneath the running process and Claude Code picks the new credentials up from disk. v2 keeps that property by introducing **slot dirs** (live mounts, swapped in-place, one per session) and demoting account dirs back to storage-only. This also deletes v1's scariest component — tmux relaunch orchestration for moves. v1 is preserved in git history (first commit on this branch, e9fd807).

## North star

Each concurrent Claude Code session runs under its **own slot dir** (`CLAUDE_CONFIG_DIR=~/claude-accounts/slot-<n>/`), and each slot holds the credentials of one account from the pool. `~/.claude/` is effectively slot zero today; this plan makes N of them. Swapping an account out is the **same in-place two-file swap cus does today, scoped to one slot dir** — the session keeps running mid-conversation, no `/exit`, no `--resume`, and the other slots (and their prompt caches) are never touched.

With N sessions spread over N accounts, each account burns ~N× slower, swaps get ~N× rarer, and a swap busts exactly one session's cache (unavoidable — different org) instead of every session's.

Success looks like: four tmux panes on four slots holding four different accounts; account A crosses its threshold; cus swaps account B's credentials into A's slot in-place; that session continues its conversation uninterrupted; the other three panes never notice. `cus status` shows pane → slot → account; `cus launch` picks the best account for every new session.

## Context / background

**Why now.** User request 2026-07-02: "on every usage swap, the cache busts... we can have four concurrent sessions running each on a different claude account, and still swap if necessary, but necessary will be 4x less frequently." Follow-up directive same day: retain the no-restart property ("howcome now we don't have to do --resume — I really like that. Can we retain that while adding this?").

**Where this sits in the decision history.** The original plan (`docs/plans/2026-05-18-claude-usage-swap.md`, decision 3) chose in-place file-copy swap over `CLAUDE_CONFIG_DIR` env-var switching so "existing tmux/shell launches just work without wrapper or alias plumbing," and explicitly listed concurrent multi-account as out of scope ("architecturally precluded by the in-place file-swap design... use AIMUX's `CLAUDE_CONFIG_DIR` pattern instead"). That framing treated the two mechanisms as rivals. The v2 insight is that they compose: **`CLAUDE_CONFIG_DIR` pins a path, not an account** — use the env var to give each session a stable private path (slot), and keep using the in-place swap to change which account occupies that path. Global mode remains a second mode, mutually exclusive per machine (`config.yaml: mode: global | per_session`, default `global`).

**Why the no-restart behavior is trusted.** In-place credential swap under live sessions is not a new bet — it has been cus's production mode since hot-swap orchestration was disabled (inbox 2026-05-19 deviation). Claude Code reads credentials from disk on subsequent requests; sessions continue across a swap. The known sharp edges — in-memory token divergence (GH #2), refreshed-token write-back clobbering (GH #3), swap crash recovery (GH #76), stale-snapshot freshness (GH #77) — all have guards built for `~/.claude/`; per_session mode reuses them scoped per slot.

**Verified mechanism (2026-07-02 research pass).** `CLAUDE_CONFIG_DIR` is real and per-process in Claude Code v2.1.197 (appears 20× in the binary; empirically isolates credentials + identity per terminal). It is undocumented-but-supported; native multi-account is an open Anthropic feature request ([anthropics/claude-code#44687](https://github.com/anthropics/claude-code/issues/44687)). Off-the-shelf survey found **no tool that combines per-session binding with usage-driven auto-swap**: AIMUX / claude-code-profiles / claude-swap do per-terminal binding with manual or command-invoked switching; cux does auto-swap but global-only; ccflare / teamclaude are proxies that rotate per-request from a shared pool (no session pinning, MITM in the auth path). And none of the env-var switchers preserve no-restart moves. Extending cus is the only path to all three properties.

**Storage roles after this plan:**

| Path | Role | Swapped in place? |
|---|---|---|
| `~/claude-accounts/account-<name>/` | Storage: canonical creds snapshot + identity + meta for one account (unchanged from today) | No — save-back target only |
| `~/claude-accounts/slot-<n>/` | Live mount: `CLAUDE_CONFIG_DIR` for one session | **Yes** — this is where swaps happen |
| `~/.claude/` | Live mount for bare launches (and the only mount in global mode) | Yes, in global mode; observe-only in per_session mode |

**Known gaps found in the current tree (2026-07-02 inventory), inherited by slot dirs:**

1. The account dirs' symlink layout (2026-05-19 unified-tree decision) is the right template for slots, but `settings.json` is a **per-dir stub** (`{"theme": "dark"}`) or absent — a session launched under such a dir gets **no hooks, no statusline, no permission allowlist**. Slots must symlink `settings.json` + `settings.local.json` to `~/.claude/`.
2. Symlink coverage drifted per dir; nobody has inventoried which of `file-history/`, `paste-cache/`, `plans/`, `session-env/`, `sessions/`, `history.jsonl` should be shared vs per-mount (Phase 1.1).
3. `.claude.json` inside each mount holds the account-bound keys (`userID`, `oauthAccount`) **plus** ~37 non-account keys (MCP registrations, per-project state, onboarding flags) that diverge across N live copies without a sync step.

## Interlock with global mode

- `mode: global` (default): today's behavior, untouched. All existing tests keep passing unmodified.
- `mode: per_session`: the daemon **never** writes `~/.claude/`. Bare `claude` launches (no wrapper) still read `~/.claude/` and keep working on whatever account it holds — observed and flagged, not swapped (swapping it is exactly the every-session cache bust this plan eliminates; see Phase 3.4).
- Mode transitions are explicit commands with validation (Phase 4). No automatic flipping.

## Parallelism map

- **Phase 1.1 (shared-state inventory)** is read-only and independent — safe immediately.
- **Phase 1.2 (slot scaffolding)** depends on 1.1. **Phase 1.3 (`.claude.json` sync)** depends on 1.1 only for the key list; parallel with 1.2.
- **Phase 2 (slot-parameterized swap + launch wrapper)** — 2.1 (swap parameterization) is independent of Phase 1 and can start first; 2.2/2.3 depend on Phase 1.
- **Phase 3 (per-slot decision loop)** depends on Phase 2.
- **Phase 4 (mode commands, migration, docs)** depends on 1–3 but its scaffolding (config schema, validation checks) can be written in parallel with Phase 3.

### Phase 1 — Slot dirs: scaffolding + shared state

**1.1 — Shared-state inventory.** Enumerate everything Claude Code reads/writes under a config dir (strace/lsof a scratch session under a scratch `CLAUDE_CONFIG_DIR`, plus diff `~/.claude/` contents against account dirs). Classify each entry: **shared** (symlink to `~/.claude/<sub>`: at minimum `settings.json`, `settings.local.json`, `projects/`, `plugins/`, `agents/`, `skills/`, `commands/`, `hooks/`, `scripts/`, `memory/`, `plans/`), **per-mount** (`.credentials.json`, `.claude.json`, `sessions/`, `cache/`, `backups/`, `history.jsonl`?, `session-env/`?), or **don't-care**. Document the canonical table in `docs/ARCHITECTURE.md` (annotate, don't rewrite, per preserve-the-log).

Demo: the table exists; each row has a tested justification (what breaks if it's per-mount vs shared).

**1.2 — Slot scaffolding (`cus slot create/list/gc` + `cus doctor --fix-dirs`).** Slots are created on demand from the canonical layout (symlinks per 1.1 + `.claude.json` seeded by 1.3's sync). `cus doctor --fix-dirs` heals both slots and account dirs idempotently: create missing symlinks, replace stub `settings.json` with a symlink (folding any non-default stub keys into the shared file), report anything unexpected. A slot whose session has exited is reusable (`cus slot gc` reaps mounts with no live process, after saving credentials back to the owning account dir).

Demo: `cus slot create` produces a dir under which `claude --print` runs with statusline + hooks active; `cus doctor --fix-dirs` on the current tree replaces account-03's `{"theme": "dark"}` stub; re-run is a no-op.

**1.3 — `.claude.json` non-account-key sync.** `cus sync-config [--from <source>]`: merges all non-account-bound top-level keys from a designated canonical (`~/.claude.json` by default) into each mount's `.claude.json`, preserving each mount's `userID` + `oauthAccount`. Reuses the surgical-merge machinery (`claude_json_for_config_dir()`, cus.py:~605). Runs at slot creation and at `cus launch` into a reused slot — **never** against a mount with a live session (write race with Claude Code's own rewrites; skip + warn instead). Atomic tempfile+rename as everywhere else.

Demo: register a scratch MCP server in `~/.claude.json`, run `cus sync-config`, verify it appears in every slot with each slot's `oauthAccount` untouched.

Effort estimate: 4–6 hours including the inventory.

### Phase 2 — Slot-parameterized swap + launch wrapper

**2.1 — Parameterize the swap by mount dir.** `execute_swap()` / `_execute_swap_locked()` (cus.py:2071/2093) currently hardcode `~/.claude/` + `~/.claude.json` as the live mount. Introduce a `mount` parameter (default: the legacy global mount, so global mode is bit-for-bit unchanged) covering: save-back of the outgoing account's creds/identity to its account dir, install of the target's creds, surgical `.claude.json` key merge, backup rotation (GH #79), crash journal (GH #76), and the freshness/drift guards (GH #77, GH #3) — all already exist, all become mount-relative. State gains a `slots` map: `{slot-1: {account, session_id, pane, created_ts}}`.

Demo: unit tests exercise a swap against a scratch mount dir and assert `~/.claude/` untouched; global-mode tests pass unmodified.

**2.2 — `cus launch [account] [-- <claude args>]`.** Picks an account — explicit name, or `auto`: reuse `pick_swap_target()`'s headroom scoring (cus.py:1358) over the whole pool, honoring the same preference config (GH #69 prefer-default applies here too) — then acquires a free slot (create if none), installs the account's credentials into it (a swap-into-empty-mount, same primitive as 2.1), pre-flights (doctor + sync + creds freshness), sets `CLAUDE_CONFIG_DIR=<slot dir>`, and `os.execvpe`s `claude` with pass-through args. Optional shell alias (`claude` → `cus launch auto --`) documented but not auto-installed.

**2.3 — Slot/account detection from the session side.** The SessionStart hook and `cus statusline` currently infer "the account" from global state. In per_session mode both derive it from their own environment: they run as children of the claude process, so `CLAUDE_CONFIG_DIR` is inherited — read it, map slot → account via state, fall back to global-active when unset (bare launch). For orchestration-independent ground truth, `find_live_panes()` (cus.py:4299) additionally reads `/proc/<pid>/environ` of each pane's claude process. `sessions.log` schema is unchanged (`<session-id>,<account>,<cwd>,<pane>,<ts>`); the account field just becomes trustworthy per-session. **Statusline must re-resolve slot → account every render** — the account under a slot changes across a swap while the session lives on.

Demo: two panes via `cus launch rayi1` / `cus launch rayi2`, one bare pane; `cus status` shows pane → slot → account for all three; each statusline names its own account and that account's usage (not the global active's).

Effort estimate: 5–7 hours.

### Phase 3 — Per-slot decision loop

**3.1 — Per-account thresholds → per-slot swaps.** In per_session mode, `decide_swap()` evaluates each account **with occupied slots** against its own progressive ladder (existing per-account `next_swap_at_pct` state carries over unchanged). Output: a list of (slot, from_account, to_account) moves. Target selection per move reuses `pick_swap_target()` excluding the hot account; multiple slots on the hot account fan out to different targets (best-headroom-first) rather than piling onto one. Executing a move is 2.1's in-place swap against the slot — **the session process is never touched.**

**3.2 — Swap timing, not relaunch orchestration.** The v1 tmux `/exit` + `--resume` orchestration is gone. What survives from the (disabled) hot-swap machinery is only its *timing* intelligence, applied per slot: lazy_swap defers a deferrable move while the slot's session JSONL is fresh (< `cache_window_seconds` — cache warm, mid-work); urgent triggers (hard 7d cap, 5h saturation, reactive 429) swap immediately, exactly like today's global semantics. The Stop-hook signal can optionally gate deferrable moves to turn boundaries. tmux is no longer required for moves — only for the observability surfaces that already use it.

**3.3 — Polling cadence.** Replace the active/inactive split (`polling.active_interval_seconds` / `inactive_interval_seconds`) with occupied/idle: accounts holding occupied slots poll at the fast cadence, idle pool accounts at the slow one. Mind the 429 backoff budget — with 4 accounts occupied this quadruples fast-cadence traffic vs today's single active account (same trap class as the 2026-06-19 burnout; the differential due-gate from GH #59/#86 must compose, not multiply). Rollout starts with `active_interval_seconds` relaxed (e.g. 300s) and tightens only with a week of clean 429 logs.

**3.4 — Bare-launch policy.** Sessions started outside `cus launch` sit on `~/.claude/` (whatever account it last held). Policy: **observe, never swap** — they appear in `cus status` flagged `bare`, SOS raises a note if a bare session is burning a hot account, but the daemon does not touch `~/.claude/` in this mode (swapping it moves every bare session at once — the exact cache bust this plan eliminates). The documented remedy is the shell alias from 2.2.

**3.5 — Save-back and token-refresh guards per slot.** Live sessions refresh OAuth tokens and write them into their mount's `.credentials.json`. The daemon's save-back (slot → owning account dir) runs on slot gc, before every swap-out, and periodically — reusing the GH #3 identity-match guard (tokens are saved back to the account they actually belong to, never blindly) and the GH #77 freshness comparison. This is strictly *less* hairy than today: each mount has exactly one writer session, whereas `~/.claude/` today is shared by all of them.

Demo (the north-star scenario): four panes on four slots/accounts; drive one account past its threshold (or lower its threshold in config); daemon in-place swaps that slot to the best target; the pane's conversation continues without interruption (no relaunch, transcript JSONL shows no session restart); the other three panes' mounts have unchanged mtimes; `cus status` shows the slot's new account.

Effort estimate: 5–7 hours including tests (suite currently 146 passing; target: per-mode parametrization of decision-loop tests + slot lifecycle, environ-resolver, and fan-out tests).

### Phase 4 — Mode commands, migration, guardrails, docs

**4.1 — `cus mode per-session` / `cus mode global`.** Explicit transitions with validation: per-session requires every pool account to pass doctor + creds freshness, creates the first slot(s), and (optionally) offers the shell alias; global saves all slots back to their account dirs, gc's them, and re-installs the chosen account (default: last global active) into `~/.claude/`. Both print a summary of what changed. Walk-back is always `cus mode global` — it restores today's exact behavior because global-mode code paths are untouched.

**4.2 — Guardrail interplay audit.** Sweep the global-mode features for per_session semantics: burn_before_reset / defer_swap_near_5h_reset (apply per-account), hard_7d_cap_pct (per-account force-move of its slots), SOS conditions, GH #2 drift detection (reframed: slot's live identity vs state's slot→account map), 429 reactive swap (attribute the 429 to the right slot via the session that hit it).

**4.3 — Docs + README.** ARCHITECTURE.md (annotated update: the "cannot run two accounts simultaneously" line gets a dated supersession note pointing here; new storage-roles table), RUNBOOK (launch/slot/mode/doctor flows), TROUBLESHOOTING (bare sessions, sync skips, symlink breakage, orphaned slots), README feature matrix row.

Effort estimate: 4–6 hours.

## Risks / open questions

1. **`CLAUDE_CONFIG_DIR` is undocumented.** Anthropic could change semantics in any release. Mitigation: it's load-bearing for a large tool ecosystem (AIMUX, claude-swap, profiles) and an open feature-request thread; doctor validates behavior empirically after Claude Code updates. Global mode remains the fallback.
2. **In-place swap under a live session is empirically trusted, not contractually.** Claude Code's exact credential-reload timing is unobserved internals: a session may keep using its in-memory access token for a while after the swap (GH #2's drift observation) before requests land on the new account. Production experience since 2026-05-19 says this settles correctly; the per-slot save-back guards (3.5) cover the write-back races. If a future Claude Code version caches credentials for the process lifetime, per_session mode degrades to v1's relaunch approach — which is why the Stop-hook gating (3.2) stays in the codebase.
3. **`.claude.json` divergence between syncs.** MCP servers registered mid-session in one mount propagate to siblings only at next slot creation/launch sync. Accepted for v1; a file-watcher sync is deliberate non-scope (write races with N live writers).
4. **Poll volume.** 4–5 accounts at fast cadence ≈ 100–130 polls/hour against the OAuth usage endpoint. Rollout starts relaxed (3.3); the memory-documented burnout trap says don't shorten intervals to compensate for anything.
5. **Slot sprawl.** Every `cus launch` can create a slot; without gc discipline `~/claude-accounts/` accumulates dead mounts holding real credentials. `cus slot gc` runs in the daemon loop, and doctor flags orphans.
6. **Multi-machine.** Unchanged scope: this plan is single-machine, same as global mode.
7. **7-day windows.** `seven_day: false` on the swap trigger is deliberate (memory: fix via `hard_7d_cap_pct`); per_session mode inherits that stance per-account, no change proposed here.

## Out of scope

- Proxy-based architectures (ccflare/teamclaude style) — rejected: per-request pool rotation defeats session-pinned prompt cache, and MITM in the auth path is a new trust surface.
- Auto-installing the `claude`→`cus launch` shell alias (documented opt-in only).
- Windows/macOS (unchanged from v1 scope).
- Cross-account load-balancing of *subagent* traffic within one session — a session is one account, full stop.
- Live migration of a *session process* between slots — sessions keep their slot for life; accounts move through slots, not the reverse.
