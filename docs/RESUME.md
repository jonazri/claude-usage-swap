# RESUME.md — durable fleet-resume runbook (panes · transcripts · sessions)

Born 2026-09-16, the day the machine crashed mid-fleet (6 panes) ahead of a
resize+reboot. Written from the practices already proven in watch.md (dead-pane
recovery, nudge discipline) and RUNBOOK.md (hot-swap orchestrator, `cus launch
-- --resume`). This file is the ONE place to open after any crash, reboot, or
resize.

## The durability model — what survives what

| Thing | Survives claude exit? | Survives reboot? | Where it lives |
| --- | --- | --- | --- |
| Transcripts (the session state) | YES | YES | `<config_dir>/projects/<cwd-encoded>/<session-id>.jsonl` — and the projects store is SHARED across ~/.claude and every `/home/rayi/claude-accounts/slot-N` (login-family mounts), so ANY slot/account can resume ANY session. Mirrored to ~/claude-history (git). |
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
   inbox.md. Worked example: zajac's Shabbos-build timer — the prompt says
   "the spec is docs/plans/2026-09-15-review-pipeline.md", so the timer is
   rebuildable from one line in memory. A timer whose prompt exists only
   in-session is a timer you've already lost.
3. **Commit ledgers/plans to the repo**, not only scratchpads. Scratchpad =
   convenience copy.
4. Keep nothing unique in gitignored paths of disposable worktrees
   (`git worktree remove` deletes ignored files invisibly — 2026-09-16 lesson).

## Post-crash / post-reboot recovery (in order)

**0. Disk sanity first:** `df -h /` and inode check on the offload volumes
(sdb = /mnt/volume_nyc1_1783020202575 had ZERO inodes as of 2026-09-16 — reads
ok, writes fail; use sda = /mnt/volume_nyc1_1777864675482/offload/).

**1. cus first:** `python3 ~/repos/claude-usage-swap/cus.py doctor` then
`... status`. Fix mounts/creds before launching anything (a resumed session
with dead creds just walls immediately).

**2. Relaunch the watchdog** (cus1a) per skills/watch.md — it should RE-CHECK
account ground truth before acting on any pre-crash plan (a planned switch may
be moot after the downtime).

**3. Per pane — classify before touching** (watch.md rules, condensed):
- Pane exists, bottom line is a shell prompt (`❯` under `rayi in …`) → claude
  is DEAD in a live pane → resume in place.
- Pane exists, claude running → LIVE. Do NOT kill/relaunch; nudge only if
  stalled ("Keep going with your task autonomously…", signed
  `[automated — NOT from Rayi]`, Enter as a separate keystroke, then read the
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

## Crash manifest — 2026-09-16 ~17:28 UTC (worked example, and live for THIS recovery)

| pane | state at 17:50 | cwd | session id | resume |
| --- | --- | --- | --- | --- |
| 2zajac2a 1.1 | claude LIVE (survived) | ~/repos/zajac | 82dd63da-c3de-430f-88d7-eb1bb56a3513 | nothing now; post-reboot: `cus launch <acct> -- --resume 82dd63da-…` from ~/repos/zajac. First duty: recreate the Shabbos-build timer (see zajac memory "CRASH-RESUME BRIEF 2026-09-16"). |
| cus1a 1.1 | claude DEAD (bash) | ~ (launched from ~/repos/claude-usage-swap) | 146dc334-03d9-4103-9dac-9a2c97a8057c | resume per step 5; it was holding for a 17:50 `cus switch rayi1` — tell it to RE-CHECK ground truth first (rayi2 was 5h≈92%). |
| yudi1a 1.1 | claude DEAD (bash) | ~ | likely 9f89e99d-66fb-46df-9b53-e6bb613cfad4 (newest -home-rayi, 04:01) — VERIFY by content-match before resuming | step 4 content-match, then step 5. |

## Optional machinery (not built — build if manual recovery gets old)

A 10-minute user-cron that snapshots `tmux list-panes -a` + each pane's claude
child's CLAUDE_CONFIG_DIR (from /proc/<pid>/environ) + newest transcript id
into a JSON manifest on sda offload, plus a `resume-fleet` script that replays
the newest manifest post-reboot (dry-run default). `cus check-orchestrate`
already computes the live half; the delta is persisting it. If built, home it
here under scripts/ and document it in this file.
