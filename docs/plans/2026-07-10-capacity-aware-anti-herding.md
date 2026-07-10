# Capacity-aware anti-herding routing

- **Date:** 2026-07-10
- **Status:** spec approved in design review; implementation not started
- **Scope:** fix #3 of the 2026-07-10 overnight-halt incident (strategy/config). Fixes #1 (halted-session revival) and #2 (event-driven reactive 429) are separate, follow-on specs.

## Incident context (why now)

On the night of 2026-07-09→10, two live tmux sessions halted on 429 and stayed dead ~7 hours despite the cus daemon running and swapping. Root causes:

1. **Revival gap (fix #1, out of scope here):** per_session lane moves swap creds on disk and log "session continues uninterrupted", but a session already at Claude Code's terminal rate-limit state (modal dialog / ended turn) never issues another API call. `hot_swap_orchestrate` (the only revival path) is gated off.
2. **Herding (this spec):** all lanes repeatedly converged onto the single "best" account, saturating accounts serially until the ladder had no valid target, then riding the last account to 100%.

At the halt hour, the fleet had **24.6 pro-units of capacity remaining** (pro-unit = one 1x-Pro-equivalent; a fresh 5x account = 5 pro-units, so ≈ 5 untouched 5x accounts' worth) — capacity was never the problem; routing was.

## Evidence (5.5 days of decisions.jsonl, 896 cycles; analysis script: `experiments/herding-analysis/herding_analysis.py`)

- Median 4 live lanes across 6 rotation accounts, yet **89% of cycles had ≥2 lanes stacked on one account** (74% ≥3, 22% ≥4) while other accounts idled.
- **Mechanism** (all confirmed in code, not inferred):
  - `_target_would_immediately_re_trip` (cus.py ~4417) rejects any target at ≥ `steps[0]` = 50% effective (max of 5h/7d). On a busy night every account crosses 50% early, so the fan-out re-pick in `decide_slot_swaps` finds nothing "healthy" and falls through to **pool double-book** (#109) — all lanes stack onto the primary pick.
  - `spread_lanes.cluster_penalty` (default 40, on) only reorders *within* the healthy candidate set; it cannot rescue across the health line, because pruning happens before scoring.
  - `spread_lanes.max_stack` is enforced only for `burn_before_reset` moves; ladder/reactive moves may stack freely.
  - `lazy_swap` defers non-urgent (spreading) moves while caches are warm; the moves that finally fire are urgent — exactly the ones allowed to stack.
- **Counterfactual on the 79 actual double-book swaps** — a distinct idle target existed below health line X in: 10% (X=50, current), 27% (60), **43% (70)**, 81% (80).
- **Tier-blindness:** routing compares percent-of-own-cap only. The fleet is heterogeneous: 4 accounts at `default_claude_max_5x`, 2 at `default_claude_max_20x` — the two 20x accounts hold **⅔ of total fleet capacity**. In absolute per-lane headroom terms, **62% of the 79 stacking swaps had an idle alternative strictly better than the chosen target**; the other 38% (mostly stacks onto a 20x) were already absolute-optimal — so naive one-lane-per-account spreading would also be wrong.
- Burn tails: p90 single-account burn ≈ 90–110 %/hour under load — a stacked account can cap within 1–2 poll cycles.

## Goals

1. Lanes distribute across accounts in proportion to *absolute remaining capacity*, not raw percent.
2. Fan-out stays functional deep into heavy nights (health line that doesn't collapse the candidate pool).
3. No behavior change for homogeneous fleets (upstream compatibility), gated and default-off in code.
4. Fewer, later, cache-friendlier swaps (aligned with the lazy_swap cost model, GH #56).

## Non-goals

- Reviving halted sessions (fix #1) and sub-cycle 429 reaction (fix #2).
- Changing `independent_logins.pool_size`, per-model weekly gates, lazy_swap/burn_before_reset semantics, or hot_swap.
- Cross-machine coordination.

## Design — Part A: config changes (`~/claude-accounts/config.yaml`)

```yaml
strategy: smart                 # was lowest_usage — adds reset-proximity + weighted 5h/7d headroom
thresholds:
  steps: [70, 85, 94]           # was [50, 75, 90] — raises leave-ladder AND target-health line
spread_lanes:
  enabled: true                 # explicit (was implicit default)
  cluster_penalty: 60           # was default 40 — idle account wins within healthy pool
poll_interval_seconds: 300      # was 600 — halves worst-case reaction latency until fix #2
capacity_aware:
  enabled: true                 # Part B, below
```

`steps[0]` intentionally does triple duty and all three move coherently to 70: (a) per-account leave-ladder start (`next_swap_at_pct`), (b) target health line, (c) launch-allocator acceptance line. Chosen over [80,90,96] (collides with `hard_7d_cap_pct: 80`; one poll-cycle burn spike can cap an account from 80) and [60,80,92] (only 27% rescuable).

## Design — Part B: capacity-aware routing (code, gated `capacity_aware.enabled`, default off)

### Normalization model

- `capacity_x(account)`: parsed from `claudeAiOauth.rateLimitTier` in the account's stored credentials (regex `_(\d+)x$` → int) at poll time, cached in `state.json` per account. `accounts[].capacity_x` in config overrides. Unparseable/unknown → baseline (conservative: never overestimate).
- `baseline_x = min(capacity_x over enabled accounts)`.
- Absolute headroom, in **baseline units**:
  `remaining_units(window) = (100 − pct) / 100 × capacity_x / baseline_x`

**Invariant (the compatibility anchor):** on a homogeneous fleet — all accounts the same size, whatever that size — every formula below reduces algebraically to today's percent behavior, bit-for-bit. Capacity-awareness only changes decisions where sizes differ. This is why `baseline_x` is fleet-relative (min), not hardcoded: an all-1x fleet gets today's behavior too, instead of an unreachable health line.

### What converts vs. what stays percent

| Concern | Space | Rationale |
|---|---|---|
| Target scoring (who to land on) | units | What matters is tokens a lane can burn before cap |
| Health line / would-re-trip / launch-accept | units | "Room for one more lane" is an absolute question |
| Leave-ladder (`steps`, `next_swap_at_pct`) | percent | Proximity to the account's *own* cap; % burn already runs 4× slower on a 20x |
| Burn-rate estimator, hysteresis, growth gates | percent | Per-account dynamics are self-consistent; convert at comparison time only |
| `hard_7d_cap_pct`, per-model weekly gate | percent | Policy lines protecting each account's week |

### The three formula changes

1. **Score density (smart and lowest_usage), per-lane:**
   `score' = strategy_score × (capacity_x / baseline_x) ÷ (lanes_on_it + 1)`
   where `lanes_on_it` = live lanes + this-cycle claims (the existing `_lane_load` input). The `÷(lanes+1)` models contention — the incoming lane shares remaining capacity with lanes already there (this is the exact quantity behind the 62% counterfactual). Scaling the whole smart score also scales the burn-soon bonus with size — intended: the capacity wasted by an unburned soon-resetting 20x is 4× a 5x's.
   For `lowest_usage` (ascending fullness sort) the equivalent form is: sort descending by `remaining_units ÷ (lanes+1)`, cluster penalty folded in as below.
2. **Cluster penalty, capacity-scaled:**
   `penalty' = cluster_penalty × lanes ÷ (capacity_x / baseline_x)`
   Density division does the capacity math; the penalty's remaining job is blast-radius isolation (co-located lanes share fate on a 429/token failure) — 60 pts/lane on a 5x, effectively 15 on a 20x.
3. **Health line (absolute `steps[0]`), reused by fan-out retry and launch allocator:**
   `healthy ⇔ remaining_units ≥ (100 − steps[0]) / 100`
   ("at least what a baseline account sitting exactly at steps[0] would have left" — 0.3 units at steps[0]=70.) A 20x at 88% (0.48u) remains a legal target; a 5x at 88% (0.12u) does not. Extrapolated percent (`_account_estimated_effective_pct`) still feeds the percent side before conversion.

### Code touch points

| Site | Change |
|---|---|
| poll path (`AccountUsage` build) | parse + cache `capacity_x` |
| new helpers | `_capacity_x(name, state, config)`, `_baseline_x(state, config)`, `_remaining_units(acct, ...)` |
| `_lane_load_penalty` (~4465) | scale by `÷ (capacity_x/baseline_x)` when gated on |
| `pick_swap_target` (~4490): lowest_usage sort, headroom score, smart score | density form when gated on |
| `_target_would_immediately_re_trip` (~4417) | absolute form when gated on |
| launch allocator acceptance (~2418) | same absolute health predicate |
| `cus status` | show `capacity_x` and units remaining (display only) |

All changes additive and behind `capacity_aware.enabled` (default **false**) so upstream merges stay bit-for-bit; our config enables it.

## Predicted effect (replay of 2026-07-09→10)

Lanes distribute ~2+2 on the 20x accounts with 5x accounts absorbing singles; every stack event after 01:00Z had ≥7 pro-units (≥1.4 baseline-units) available on a max account below the absolute health line; with 24.6 pro-units of fleet headroom at 07:00Z no account needed to reach 100% before its reset. Residual genuine-exhaustion 429s remain possible and are the domain of fixes #1/#2.

## Rollout & verification

1. **Unit tests** (beside existing strategy tests in `tests/`):
   - Homogeneous-reduction invariant: with `capacity_aware.enabled`, an all-same-x fleet produces identical picks to gate-off across recorded scenarios.
   - Heterogeneous fixtures: 20x@65% beats idle 5x@60%; loses once 3 lanes sit on it; health line admits 20x@88%, rejects 5x@88%.
   - Multiplier sourcing: tier parse, config override, unknown→baseline.
2. **Dry-run against live state:** `cus daemon --once --no-execute` before and after the config change; diff the decisions.
3. **Apply:** update config, `systemctl --user restart cus.service`.
4. **Observe 24–48h:** re-run `experiments/herding-analysis/herding_analysis.py`; success = double-book swaps with a strictly-better idle alternative (the 62% metric) drops to ~0, and max-stack≥3 cycles fall from 74% to near per-capacity-expected levels.

## Risks and accepted edges

- **Overshoot between polls:** steps[0]=70 + p90 burns ~90–110%/h ⇒ up to ~8pp overshoot per 300s cycle; mitigated by the extrapolating estimator, fully addressed by fix #2.
- **Max-account over-stacking:** density division and physical `pool_size: 4` cap stack depth; accepted by design (that's what capacity proportionality means).
- **Baseline shift:** adding a smaller account later lowers `baseline_x` and loosens the absolute health line fleet-wide by the same ratio. Inherent to fleet-relative anchoring; documented, accepted.
- **Tier string drift:** if Anthropic renames `rateLimitTier` values, parse fails → account treated as baseline (safe); config override available.
- **Two changes at once** (smart + capacity-aware): accepted by operator choice in design review; the invariant tests isolate capacity-aware regressions, and `decisions.jsonl` records strategy per decision for attribution.
