# Capacity-aware anti-herding routing

- **Date:** 2026-07-10
- **Status:** revision 2 — committee round-1 findings applied (accept/leave consistency, single units-space ranking key, invariant restated, baseline definition hardened); implementation not started
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
3. No behavior change with the gate off (upstream compatibility); with the gate on, homogeneous fleets diverge from today only where lane contention exists (see Invariant).
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
  cluster_penalty: 60           # gate-on: 0.60 units/lane isolation preference (see formula 1)
poll_interval_seconds: 300      # was 600 — halves worst-case reaction latency until fix #2
capacity_aware:
  enabled: true                 # Part B, below
```

**Deployment ordering (committee round-1):** Part B code lands first with the gate off (default), tests green, dry-run clean. Part A is then applied as one atomic config edit (including `capacity_aware.enabled: true`) followed by a daemon restart. The config block is never applied against a daemon that lacks Part B code.

`steps[0]` intentionally does triple duty and all three move coherently to 70: (a) per-account leave-ladder start (`next_swap_at_pct`), (b) target health line, (c) launch-allocator acceptance line. Chosen over [80,90,96] (collides with `hard_7d_cap_pct: 80`; one poll-cycle burn spike can cap an account from 80) and [60,80,92] (only 27% rescuable).

## Design — Part B: capacity-aware routing (code, gated `capacity_aware.enabled`, default off)

### Normalization model

- `capacity_x(account)`: parsed from `claudeAiOauth.rateLimitTier` in the account's stored credentials (regex `_(\d+)x$` → int, e.g. `default_claude_max_20x` → 20). Read at poll time by a new sibling of `_read_access_token_with_expiry` (which returns only token/expiry today) and cached per account in `state.json`. `accounts[].capacity_x` in config overrides the parsed value.
- `baseline_x`: **two-pass, guarded** (committee round-1 — the one-pass definition was circular):
  1. `baseline_x = min(capacity_x over enabled accounts with a parseable/overridden tier)`, floored at 1.
  2. Accounts with no parseable tier and no override are then assigned `capacity_x = baseline_x` (ratio 1 — they behave exactly as under today's percent logic; this is *neutral*, not "conservative": if such an account's true tier is below the fleet minimum, the operator must set `accounts[].capacity_x`).
  3. If **no** account has a parseable tier, `baseline_x = 1` and every ratio is 1 — the gated math reduces to pure percent behavior fleet-wide.
- Absolute headroom, in **baseline units**:
  `remaining_units(acct) = (100 − pct) / 100 × capacity_x / baseline_x`
  (`pct` = the effective percent already used by the ladder: max of enabled windows, extrapolated via `_account_estimated_effective_pct`. `baseline_x ≥ 1` by construction — no zero division.)

**Invariant (restated after committee round-1):** with the gate **off** (default), behavior is bit-for-bit today's — everything below is gated. With the gate **on**, on a homogeneous fleet (all ratios 1): the unit conversions and threshold comparisons reduce exactly to today's percent forms **at zero lane-load**; where lane loads exist, the contention divisor `÷(lanes+1)` intentionally replaces today's linear cluster penalty — that is a designed behavioral change, not a regression, and the invariant tests are scoped accordingly (see Rollout).

### What converts vs. what stays percent

| Concern | Space | Rationale |
|---|---|---|
| Target scoring (who to land on) | units, per-lane | What matters is tokens a lane can burn before cap |
| Health line / would-re-trip / launch-accept | units, per-lane | "Room for one more lane" — divided by the lanes already there |
| Leave-ladder trip (`steps` / `next_swap_at_pct`) | **percent thresholds, compared in units** (account-level, no lane divisor) | Committee round-1: comparing raw percent while accepting in units re-opens the swap-back loop (a 20x lands legally at 80%, then trips its 70-step next cycle). Converting the comparison keeps accept/leave consistent: a 20x trips its 70-step at 92.5%, leaving the same absolute buffer (1.5 pro-units) a 5x has at 70. |
| Burn-rate estimator, hysteresis, growth gates | percent | Per-account dynamics are self-consistent; convert at comparison time only |
| `hard_7d_cap_pct`, per-model weekly gate | percent | Policy lines protecting each account's week |

### The formula changes (all gated; all comparisons in baseline units)

Let `ratio = capacity_x / baseline_x`, `lanes = live lanes on the candidate + this-cycle claims` (the existing `_lane_load` input).

1. **Ranking key — one combined expression, all scoring strategies** (`lowest_usage`, `headroom`, `smart`):
   `key = (strategy_score_pts / 100) × ratio ÷ (lanes + 1) − (cluster_penalty / 100) × lanes`
   where `strategy_score_pts` is the strategy's existing point-scale score (for `lowest_usage`, use `100 − effective_pct`, preserving its ordering semantics; 5h tiebreak unchanged). Candidates rank by `key` descending. Both terms are in baseline units: the first is per-lane capacity density (the exact quantity behind the 62% counterfactual — scaling smart's burn-soon bonus with size is intended, since the capacity wasted by an unburned soon-resetting 20x is 4× a 5x's); the second is the isolation preference (co-located lanes share fate on a 429/token failure), worth 0.60u per lane at `cluster_penalty: 60`. The old point-scale subtraction (`_lane_load_penalty`) is **not** applied in gate-on mode — mixing point-scale penalties with unit-scale scores produced provably wrong orderings (committee round-1: a one-lane 20x at 99% outranked a one-lane idle 5x).
   *Worked example (isolation-vs-capacity dial):* idle 5x@60% keys 0.55; 20x@65% with 1 lane keys (0.35×4)/2 − 0.6 = 0.10 — isolation wins. Same 20x idle keys 1.40 — capacity wins. `cluster_penalty` (in hundredths of a unit per lane) is the knob that moves this crossover.
2. **Health line (absolute, per-lane, strict), reused by fan-out retry and launch allocator:**
   `healthy ⇔ remaining_units ÷ (lanes + 1) > (100 − steps[0]) / 100`
   Strict `>` preserves today's boundary semantics (currently healthy ⇔ pct **<** steps[0]; committee round-1 caught the `≥` flip). The `÷(lanes+1)` term closes the round-1 gap where a 20x@88% carrying 3 lanes passed a gate whose rationale is "room for one more lane" — its per-lane share (0.12u) is exactly the rejected 5x@88% case. Homogeneous zero-lane reduction: healthy ⇔ pct < steps[0], as today.
3. **Leave-ladder trip (absolute, account-level):**
   `trip ⇔ remaining_units ≤ (100 − next_swap_at_pct) / 100`
   The ladder's percent *thresholds* (`steps`, per-account `next_swap_at_pct` ratchet, `maybe_reset_thresholds` unwind) are untouched; only `decide_swap`'s Trigger-2 comparison converts. `≤` preserves today's trip-at-equality boundary (pct ≥ threshold). No lane divisor here — leaving is about the account's own proximity to cap. Accept (2) is deliberately *stricter* than leave (3); that ordering is loop-safe, the reverse is what ping-pongs.

### Code touch points

| Site | Change |
|---|---|
| poll path (`poll_account_usage`) | new `_read_rate_limit_tier(account_name)` beside `_read_access_token_with_expiry` (cus.py ~3192, currently token/expiry only); parse + cache `capacity_x` in state |
| new helpers | `_capacity_ctx(state, config)` → `{baseline_x, capacity_x_by_name}`; `_remaining_units(acct, name, ctx, config)` |
| **plumbing** (committee round-1) | `_target_would_immediately_re_trip(acct, config)` and `_launch_candidate_saturated(acct, config)` receive no fleet state today (call sites cus.py 2436, 4679, 4700, 7750, 9808 pass acct/config only). Thread the context via the established shim pattern (`state["_lane_load"]` precedent): decision-layer callers stash `state["_capacity_ctx"]`; the two helpers gain an optional `ctx=None` param — `None` ⇒ today's percent path, so non-lane callers (SOS probes, `cus switch`, global mode) are untouched |
| `pick_swap_target` (~4490): lowest_usage sort, headroom score, smart score | gate-on: rank by formula 1's combined key; `_lane_load_penalty` bypassed (returns 0) in gate-on mode |
| `decide_swap` Trigger 2 (~7191) | gate-on: formula 3 comparison |
| launch allocator acceptance (~2418) | gate-on: formula 2 predicate |
| SOS | warn when `baseline_x` changes between cycles (mitigates the baseline-shift edge below) |
| `cus status` | show `capacity_x` and units remaining (display only) |

All changes additive and behind `capacity_aware.enabled` (default **false**) so upstream merges stay bit-for-bit; our config enables it.

## Predicted effect (replay of 2026-07-09→10)

Lanes distribute ~2+2 on the 20x accounts with 5x accounts absorbing singles; every stack event after 01:00Z had ≥7 pro-units (≥1.4 baseline-units) available on a max account below the absolute health line; with 24.6 pro-units of fleet headroom at 07:00Z no account needed to reach 100% before its reset. Residual genuine-exhaustion 429s remain possible and are the domain of fixes #1/#2.

## Rollout & verification

1. **Unit tests** (beside existing strategy tests in `tests/`; fixtures are synthetic state dicts — hand-built from decisions.jsonl snapshots where noted — committed under `tests/fixtures/capacity_aware/`):
   - Gate-off: identical picks to current code across all fixtures (bit-for-bit).
   - Gate-on homogeneous, zero lane-load: identical picks to gate-off (the reduced invariant), including boundary fixtures at `pct == steps[0]` (healthy=false) and the Trigger-2 equality boundary (trip=true).
   - Gate-on homogeneous, loaded: explicit divergence fixtures asserting contention-division ordering (documents the designed change).
   - Heterogeneous, per strategy (`smart` and `lowest_usage` separately, 5h and 7d driven variants): 20x@65% idle beats 5x@60% idle; loses to it once the 20x carries 1 lane (worked example above); health line admits idle 20x@88%, rejects 20x@88%+3 lanes and 5x@88%; ladder fixture: 20x@80% with `next_swap_at_pct=70` is accepted AND does not trip Trigger 2 (trips at 92.5%).
   - Baseline sourcing: tier parse, config override, unknown→ratio-1, all-unknown→all-ratio-1, floor at 1, single-enabled-account fleet.
2. **Dry-run against live state:** `cus daemon --once --no-execute` before and after the config change; diff the decisions.
3. **Apply** (ordering per Part A): Part B merged with gate off → tests + dry-run → single atomic config edit → `systemctl --user restart cus.service`.
4. **Observe 24–48h:** re-run `experiments/herding-analysis/herding_analysis.py`; success criteria (numeric): the capacity-aware counterfactual metric (§6 of the script — double-book swaps with a strictly-better idle alternative in per-lane units) drops from **62% to <5%**, and no 429-halt of a live session while ≥1 baseline-unit of per-lane headroom exists on any healthy account.

## Risks and accepted edges

- **Overshoot between polls:** a 20x now rotates at 92.5% of its own cap — by construction the same 1.5-pro-unit absolute buffer a 5x has at 70%, but p90 burn tails (~90–110%/h of a 5x-sized window) consume absolute units at the same rate regardless of host account; the extrapolating estimator mitigates, fix #2 closes the tail.
- **Max-account stacking:** allowed when per-lane density genuinely favors it; bounded by the health line's per-lane form and the physical `pool_size: 4` family cap.
- **Baseline shift:** adding a smaller account later lowers `baseline_x` and loosens the absolute health line fleet-wide by the same ratio (a 1x joining a 5x-baseline fleet moves a 5x's effective line from 70% to 94% of its own cap). Inherent to fleet-relative anchoring; mitigated by the SOS baseline-change warning (touch points) so it is never silent; operator response is re-tuning `steps` or overriding `capacity_x`.
- **Tier string drift:** if Anthropic renames `rateLimitTier` values, parse fails → ratio 1 (neutral, percent behavior); config override available.
- **Two changes at once** (smart + capacity-aware): accepted by operator choice in design review; the gate-off/gate-on test matrix isolates capacity-aware regressions, and `decisions.jsonl` records strategy per decision for attribution.
