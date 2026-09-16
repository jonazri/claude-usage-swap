# RESUME.md — durable fleet-resume runbook (panes · transcripts · sessions)

Born 2026-09-16, the day the machine crashed mid-fleet (several panes) ahead of a
resize+reboot. Written from the practices already proven in watch.md (dead-pane
recovery, nudge discipline) and RUNBOOK.md (hot-swap orchestrator, `cus launch
-- --resume`). This file is the ONE place to open after any crash, reboot, or
resize.

> Account names, session ids, tmux pane names, project paths, and machine infra
> IDs in this doc are **placeholders** (`acct-A`, `<session-id>`, `sess-A`,
> `~/repos/<project>`, `/mnt/<offload-volume>`). Substitute your own; the live
> specifics for a given box live in the operator's private notes, not here.

## The durability model — what survives what

| Thing | Survives claude exit? | Survives reboot? | Where it lives |
| --- | --- | --- | --- |
| Transcripts (the session state) | YES | YES | `<config_dir>/projects/<cwd-encoded>/<session-id>.jsonl` — and the projects store is SHARED across `~/.claude` and every `~/claude-accounts/slot-N` (login-family mounts), so ANY slot/account can resume ANY session. Mirrored to a git history repo. |
| Pushed git work | YES | YES | origin. The standing "push often" rule is the real safety net. |
| tmux panes | YES (pane shows bare shell) | NO | tmux server memory. Cheap to recreate — a pane is just cwd + config dir + `claude --resume`. |
| cus account/lane state | YES | YES | cus state.json + account dirs. `cus doctor` heals. |
| Session-local timers (CronCreate), watches, background tasks | NO — die with the claude process | NO | Nowhere. THE ONLY UNRECOVERABLE CLASS. Mitigation below. |
| Unpushed commits / dirty worktrees / gitignored files | YES | YES (disk) | Worktrees under scratchpads. Find them; don't assume. |

## Pre-crash discipline (every long-running session, continuously)

1. **Push often** (cross-repo rule). Unpushed work is invisible and fragile.
2. **Journal every session-local timer** the moment you create it: what fires
   when, and the full recreation prompt (or a pointer to a repo-committed spec
   it can be rebuilt from) — into the session's auto-memory AND the repo's
   inbox.md. Worked example: a project's scheduled-build timer whose journalled
   prompt says "the spec is `docs/plans/<date>-<slug>.md`", so the timer is
   rebuildable from one line in memory. A timer whose prompt exists only
   in-session is a timer you've already lost.
3. **Commit ledgers/plans to the repo**, not only scratchpads. Scratchpad =
   convenience copy.
4. Keep nothing unique in gitignored paths of disposable worktrees
   (`git worktree remove` deletes ignored files invisibly — 2026-09-16 lesson).

## Post-crash / post-reboot recovery (in order)

**0. Disk sanity first:** `df -h /` and an inode check (`df -i`) on any offload
volumes (one offload volume had ZERO free inodes once — reads ok, writes fail;
switch to a volume with inodes free).

**1. cus first:** `python3 ~/repos/claude-usage-swap/cus.py doctor` then
`... status`. Fix mounts/creds before launching anything (a resumed session
with dead creds just walls immediately).

**2. Relaunch the watchdog** per skills/watch.md — it should RE-CHECK
account ground truth before acting on any pre-crash plan (a planned switch may
be moot after the downtime).

> **Annotation 2026-09-16:** after a reboot the watchdog was re-established as a
> **FRESH session** in tmux session `cus-watchdog`, parked **locked on a
> Fable-dead, standard-pool account** (the correct Opus park). The prior
> watchdog pane was retired (its loop died on the reboot; the pane became an
> interactive ops chat). When re-homing the watchdog after a crash/reboot, use
> the **new-pane FRESH-session handoff** documented in
> `skills/watch.md` §"Update 2026-09-16 — Migrating / re-homing the watchdog" —
> do NOT resume the same session id in a second live pane (transcript-write
> conflict), and verify the target account has a free independent login family
> (`cus login-mount` if not; a canonical relogin alone does NOT provision one —
> GH #190/#104).

**3. Per pane — classify before touching** (watch.md rules, condensed):
- Pane exists, bottom line is a shell prompt (`❯` under `<user> in …`) → claude
  is DEAD in a live pane → resume in place.
- Pane exists, claude running → LIVE. Do NOT kill/relaunch; nudge only if
  stalled ("Keep going with your task autonomously…", signed
  `[automated — NOT the operator]`, Enter as a separate keystroke, then read the
  pane to confirm it submitted).
- Pane gone (post-reboot: all of them) → recreate:
  `tmux new-session -d -s <name> -c <cwd>`.

**4. Find each session's id** (when not already known):
- Newest transcript for the pane's cwd:
  `ls -t <config_dir>/projects/<cwd-with-slashes-as-dashes>/*.jsonl | head -1`
  (any config dir works — the store is shared).
- Ambiguous? Content-match: grep a distinctive phrase you remember from that
  session across the dir's *.jsonl.
- The filename (minus .jsonl) IS the `--resume` id.

**5. Relaunch:**
- Slotted (preferred — account picked/spread by cus):
  `cus launch <acct> -- --resume <id>` from the right cwd.
- Direct: `tmux send-keys -t <pane> 'cd <cwd> && CLAUDE_CONFIG_DIR=<slot-dir> claude --resume <id>' Enter`
- Verify the session actually came back (title/prompt), per watch.md; never
  trust the send-keys return code.

**6. Rebuild session timers:** each resumed session reads its own memory brief
(auto-memory project_status or equivalent) and recreates its CronCreate jobs.
This is why rule 2 above exists.

**7. Only then** resume normal watch cadence.

## Crash manifest — 2026-09-16 (worked example / template)

The shape to capture at crash time, one row per pane:

| pane | state at crash | cwd | session id | resume |
| --- | --- | --- | --- | --- |
| sess-A | claude LIVE (survived) | ~/repos/<project-A> | `<session-id-A>` | nothing now; post-reboot: `cus launch <acct> -- --resume <session-id-A>` from its cwd. First duty: recreate its scheduled-build timer (see that project's crash-resume memory brief). |
| cus-watchdog | claude DEAD (bash) | ~ (launched from ~/repos/claude-usage-swap) | `<session-id-B>` | resume per step 5; if it was holding for a pre-crash `cus switch`, tell it to RE-CHECK ground truth first (the target account's headroom may have changed). |
| sess-C | claude DEAD (bash) | ~ | `<session-id-C>` (VERIFY by content-match before resuming — pick the newest transcript for the cwd, then grep a distinctive phrase) | step 4 content-match, then step 5. |

## Optional machinery (not built — build if manual recovery gets old)

A 10-minute user-cron that snapshots `tmux list-panes -a` + each pane's claude
child's CLAUDE_CONFIG_DIR (from /proc/<pid>/environ) + newest transcript id
into a JSON manifest on an offload volume, plus a `resume-fleet` script that
replays the newest manifest post-reboot (dry-run default). `cus check-orchestrate`
already computes the live half; the delta is persisting it. If built, home it
here under scripts/ and document it in this file.
