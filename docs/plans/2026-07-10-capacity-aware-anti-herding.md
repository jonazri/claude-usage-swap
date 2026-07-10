# Capacity-aware anti-herding routing

- **Date:** 2026-07-10
- **Status:** revision 3 — committee round-2 findings applied (pinned `reference_x` replaces fleet-relative baseline, blanket conversion rule with enumerated gate sites, hysteresis/bbr/SOS conversions, ctx plumbing completed); implementation not started
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
3. No behavior change with the gate off (upstream compatibility); with the gate on, fleets homogeneous at the reference size diverge from today only where lane contention exists (see Invariant).
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
  reference_x: 5                # pinned unit scale (committee round-2; = our fleet's smallest tier)
```

**Deployment ordering (committee round-1):** Part B code lands first with the gate off (default), tests green, dry-run clean. Part A is then applied as one atomic config edit (including `capacity_aware.enabled: true`) followed by a daemon restart. The config block is never applied against a daemon that lacks Part B code.

`steps[0]` intentionally does triple duty and all three move coherently to 70: (a) per-account leave-ladder start (`next_swap_at_pct`), (b) target health line, (c) launch-allocator acceptance line. Chosen over [80,90,96] (collides with `hard_7d_cap_pct: 80`; one poll-cycle burn spike can cap an account from 80) and [60,80,92] (only 27% rescuable).

## Design — Part B: capacity-aware routing (code, gated `capacity_aware.enabled`, default off)

### Normalization model

- `capacity_x(account)`: parsed from `claudeAiOauth.rateLimitTier` in the account's stored credentials (regex `_(\d+)x(?=[_-]|$)` — mid-string tolerant so suffix drift like `_20x_v2` still parses; committee round-2). Read at poll time by a new sibling of `_read_access_token_with_expiry` (cus.py:3192, currently token/expiry only) and cached per account in `state.json`. `accounts[].capacity_x` in config overrides the parsed value. **Validation (committee round-2):** overrides must be numeric and ≥ 1; invalid values are dropped with an SOS note and the account falls back to parse-or-neutral. Accounts with no parseable tier and no valid override get `capacity_x = reference_x` (ratio 1 — *neutral*: they behave exactly as under today's percent logic at zero lane-load; if such an account's true tier differs, the operator sets the override).
- `reference_x`: the unit scale, **pinned in config** (`capacity_aware.reference_x`), NOT derived from fleet composition. Committee round-2 established that a fleet-relative `min()` baseline is unstable in both directions: removing/disabling the smallest account silently shrank every other account's computed headroom (a 20x@80% dropping 0.8u→0.2u trips its ladder fleet-wide), adding a smaller account silently inflated it (a 5x's trip buffer collapsing from 1.5 to 0.3 pro-units — under one p90 burn-tail poll cycle), and either shift silently rescaled the density term of formula 1 while the isolation term stayed fixed, moving the herding crossover. A pinned reference moves only by explicit, reviewable config edit. If `reference_x` is absent when the gate is first enabled, the daemon snapshots the observed fleet minimum into an SOS message instructing the operator to pin it, and uses that observed value for the cycle; each cycle where the observed fleet minimum differs from the pinned reference, SOS raises a (stateless — recomputed per cycle, nothing persisted) retune warning. `reference_x` is validated ≥ 1.
- Absolute headroom, in **reference units**:
  `remaining_units(acct) = (100 − pct) / 100 × capacity_x / reference_x`
- **`pct` source rule (committee round-2 — no redefinition):** the units conversion *wraps whatever pct expression each site computes today*. Trigger 2 keeps its raw `current_max_pct` max-ed with the look-ahead extrapolation only under poll-accel (cus.py:7213–7216); `_target_would_immediately_re_trip` keeps its unconditional `_account_estimated_effective_pct`; `lowest_usage` keeps its non-extrapolated `_account_effective_pct`. Only the final cross-account comparison converts; no pct source, look-ahead, or poll-accel behavior changes.

**Invariant (restated):** with the gate **off** (default), behavior is bit-for-bit today's — everything below is gated. With the gate **on**, for a fleet homogeneous at the reference size (all ratios 1): threshold comparisons reduce exactly to today's percent forms **at zero lane-load**; where lane loads exist, the contention divisor `÷(lanes+1)` intentionally replaces today's linear cluster penalty — a designed behavioral change, tested as such (see Rollout). Ratio-1 (unknown-tier) accounts inside a heterogeneous fleet get the same zero-lane-load equivalence, not blanket "exactly as today" (committee round-2 wording fix).

### The conversion rule

**Every comparison between two different accounts' fullness on the decision path converts to units. Every threshold about one account's own cap stays a percent, but where its comparison feeds an accept/leave/hold decision, the comparison converts.** Converting gates one at a time is how rounds 1–2 found ping-pong, hysteresis vetoes, and dead bands; the enumeration below is exhaustive for the decision path (verified against decide_swap, decide_slot_swaps, pick_swap_target, pick_launch_account, and the SOS probes):

| # | Gate (site) | Today | Gate-on |
|---|---|---|---|
| G1 | Target ranking — `pick_swap_target` scoring strategies (~4490) | points/percent sort | formula 1 key |
| G2 | Health line / re-trip — `_target_would_immediately_re_trip` (4417) + fan-out retry (7750) | pct ≥ steps[0] | formula 2 |
| G3 | Launch accept — `_launch_candidate_saturated` (2406) via 2436 | pct ≥ steps[0] | formula 2 |
| G4 | Leave trip — `decide_swap` Trigger 2 (~7191) | pct ≥ next_swap_at_pct | formula 3 |
| G5 | Swap hysteresis — `min_improvement_gate` (~7381) | hold if target_pct > active_pct − min_improvement | hold if `target_units − active_units < min_improvement/100` (committee round-2 critical: raw form vetoes a 5x@71 → idle 20x@75 move — the flagship scenario) |
| G6 | burn-before-reset Trigger 2.5 precheck (~7183) | run bbr iff pct < next_swap_at_pct | run bbr iff `remaining_units > (100 − next_swap_at_pct)/100` — exact complement of G4, closing the 70–92.5% dead band on a 20x |
| G7 | Non-scoring strategy accepts — drain (4736), strict_priority (4757) | pct < next_swap_at_pct | `remaining_units > (100 − next_swap_at_pct)/100` (latent — operator config uses smart; converted for consistency) |
| G8 | SOS / status fleet probes — premium-target loss (~9808), exhaustion/all-full probes | pct ≥ steps[0] | formula 2 with ctx (committee round-2: the percent path false-alarms "0 valid targets" on a healthy 20x@80%) |

`ctx=None` (percent path) remains only for genuinely single-account callers (`cus switch` validation, external tooling); every fleet-level decision or report threads ctx.

### The formula changes (all gated; all comparisons in reference units)

Let `ratio = capacity_x / reference_x`, `lanes = live lanes on the candidate + this-cycle claims` (the existing `_lane_load` input; on the launch path, built from `occupied_slot_accounts(state)`).

1. **Ranking key (G1) — one combined expression, all scoring strategies** (`lowest_usage`, `headroom`, `smart`):
   `key = (strategy_score_pts / 100) × ratio ÷ (lanes + 1) − (cluster_penalty / 100) × lanes`
   where `strategy_score_pts` is the strategy's existing point-scale score (for `lowest_usage`, `100 − effective_pct`, preserving its ordering semantics; 5h tiebreak unchanged). Candidates rank by `key` descending. Both terms are in reference units: the first is per-lane capacity density (the exact quantity behind the 62% counterfactual — scaling smart's burn-soon bonus with size is intended, since the capacity wasted by an unburned soon-resetting 20x is 4× a 5x's); the second is the isolation preference (co-located lanes share fate on a 429/token failure), 0.60u per lane at `cluster_penalty: 60`, and is scale-stable because `reference_x` is pinned. The old point-scale subtraction (`_lane_load_penalty`) is **not** applied in gate-on mode.
   *Worked example (lowest_usage form, `score_pts = 100 − eff`, reference_x = 5; committee round-2 corrected):* idle 5x@60% keys (40/100)×1÷1 − 0 = **0.40**; 20x@65% with 1 lane keys (35/100)×4÷2 − 0.6 = **0.10** — isolation wins; same 20x idle keys **1.40** — capacity wins. `cluster_penalty` (hundredths of a unit per lane) is the dial that moves this crossover.
2. **Health line (G2/G3/G8 — absolute, per-lane, strict):**
   `healthy ⇔ remaining_units ÷ (lanes + 1) > (100 − steps[0]) / 100`
   Strict `>` preserves today's boundary (healthy ⇔ pct **<** steps[0]). Per-lane, because the gate's rationale is "room for one more lane": an idle 20x@88% (0.48u) passes; the same account carrying 3 lanes (0.12u/lane) fails, as does a 5x@88%.
3. **Leave trip (G4 — absolute, account-level):**
   `trip ⇔ remaining_units ≤ (100 − next_swap_at_pct) / 100`
   Percent thresholds (`steps`, per-account ratchet, `maybe_reset_thresholds`) untouched; only the comparison converts. `≤` preserves today's trip-at-equality. No lane divisor — leaving is about the account's own cap proximity. Accept (2) is deliberately *stricter* than leave (3); that ordering is loop-safe, the reverse ping-pongs. A 20x trips its 70-step at 92.5% — the same 1.5-pro-unit absolute buffer a 5x has at 70%.

### Code touch points

| Site | Change |
|---|---|
| poll path (`poll_account_usage`, 3793) | new `_read_rate_limit_tier(account_name)` beside `_read_access_token_with_expiry` (3192); parse + validate + cache `capacity_x` in state |
| new helpers | `_capacity_ctx(state, config)` → `{reference_x, capacity_x_by_name, lane_load_by_name}` (lane loads from live occupancy + this-cycle claims; committee round-2 — the round-1 payload couldn't evaluate formula 2); `_remaining_units(name, acct, ctx, config)` |
| **plumbing** | `_target_would_immediately_re_trip` and `_launch_candidate_saturated` gain `(name=None, ctx=None)` params — `None` ⇒ today's percent path. Call sites 2436, 4679, 4700, 7750, 9808 all iterate name-keyed collections and pass both; decision layers stash `state["_capacity_ctx"]` (the `state["_lane_load"]` shim precedent); `pick_launch_account`'s shim builder additionally builds lane occupancy from `occupied_slot_accounts(state)` |
| `pick_swap_target` (~4490) | G1 key; `_lane_load_penalty` returns 0 in gate-on mode; drain/strict_priority accepts converted (G7) |
| `decide_swap` (~7183, ~7191, ~7381) | G6 precheck, G4 trip, G5 hysteresis |
| launch allocator (~2418) | G3 |
| SOS / status | G8 probes take ctx; stateless `reference_x`-vs-observed-min drift warning; `capacity_x` + units shown in `cus status` (display only) |

All changes additive and behind `capacity_aware.enabled` (default **false**) so upstream merges stay bit-for-bit; our config enables it.

## Predicted effect (replay of 2026-07-09→10)

Lanes distribute ~2+2 on the 20x accounts with 5x accounts absorbing singles; every stack event after 01:00Z had ≥7 pro-units (≥1.4 reference-units) available on a max account below the absolute health line; with 24.6 pro-units of fleet headroom at 07:00Z no account needed to reach 100% before its reset. Residual genuine-exhaustion 429s remain possible and are the domain of fixes #1/#2.

## Rollout & verification

1. **Characterization first (committee round-2):** before any Part B code, capture gate-off golden fixtures — recorded picks of *current* code across the fixture set — committed under `tests/fixtures/capacity_aware/`; the gate-off and gate-on-homogeneous-zero-load suites assert against these goldens rather than a hand-waved "current behavior".
2. **Unit tests** (beside existing strategy tests in `tests/`):
   - Gate-off: identical picks to goldens across all fixtures (bit-for-bit).
   - Gate-on homogeneous-at-reference, zero lane-load: identical to goldens, including boundary fixtures at `pct == steps[0]` (healthy=false) and Trigger-2 equality (trip=true), plus a **burn-rate-divergence fixture** (raw vs extrapolated pct disagree) proving the per-site pct source rule held.
   - Gate-on homogeneous, loaded: explicit divergence fixtures asserting contention-division ordering (the designed change).
   - Heterogeneous, per strategy (`smart` and `lowest_usage`, 5h- and 7d-driven variants): the worked-example triple (0.40 / 0.10 / 1.40); health admits idle 20x@88%, rejects 20x@88%+3 lanes and 5x@88%; ladder fixture: 20x@80% with `next_swap_at_pct=70` is accepted, not tripped (trips at 92.5%); **hysteresis fixture (G5): active 5x@71% moves to idle 20x@75%** (vetoed by today's raw gate); **bbr fixture (G6): 20x@80% remains bbr-eligible**; **SOS fixture (G8): 20x@80% not reported saturated**.
   - Sourcing/validation: tier parse incl. suffixed forms (`_20x_v2`), config override, invalid override → SOS + neutral, unknown → ratio-1, missing `reference_x` at first enable → snapshot SOS, drift warning, floors.
3. **Dry-run against live state:** `cus daemon --once --no-execute` before and after the config change; diff the decisions.
4. **Apply** (ordering per Part A): Part B merged with gate off → tests + dry-run → single atomic config edit → `systemctl --user restart cus.service`.
5. **Observe 24–48h:** re-run `experiments/herding-analysis/herding_analysis.py`; success criteria (numeric): the capacity-aware counterfactual metric (§6 — double-book swaps with a strictly-better idle alternative in per-lane units) drops from **62% to <5%**, and no 429-halt of a live session while ≥1 reference-unit of per-lane headroom exists on any healthy account. Two measurement caveats (committee round-2): gate-on decision `reason` strings MUST keep emitting the literal `double-book` token (compatibility contract — §6 filters on it), and the script's hardcoded tier map is to be replaced by reading `capacity_x` from state.json before the observation window.

## Risks and accepted edges

- **Overshoot between polls:** a 20x now rotates at 92.5% of its own cap — by construction the same 1.5-pro-unit absolute buffer a 5x has at 70%, but p90 burn tails (~90–110%/h of a 5x-sized window) consume absolute units at the same rate regardless of host account; the extrapolating estimator mitigates, fix #2 closes the tail.
- **Max-account stacking:** allowed when per-lane density genuinely favors it; bounded by the health line's per-lane form and the physical `pool_size: 4` family cap.
- **Reference retune is manual:** pinning `reference_x` trades silent runtime shifts (round-2 criticals) for an explicit operator decision when fleet composition changes; the stateless drift warning makes it visible every cycle until addressed.
- **Tier string drift:** unparseable `rateLimitTier` → ratio 1 (neutral percent behavior) — safe but *silent per-account*; the relaxed regex narrows this, and `cus status` showing per-account `capacity_x` makes it auditable.
- **Two changes at once** (smart + capacity-aware): accepted by operator choice in design review; the gate-off/gate-on test matrix isolates capacity-aware regressions, and `decisions.jsonl` records strategy per decision for attribution.
