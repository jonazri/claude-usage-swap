"""Gate-off golden scenario builders for the capacity-aware anti-herding
rollout (Phase 0b, spec Rollout §1: docs/plans/2026-07-10-capacity-aware-anti-herding.md).

Each scenario is a (name, state, config) triple exercised against UNMODIFIED
cus.py: a representative sample of fleets / strategies / ladder positions run
through `pick_swap_target` and `decide_swap`, whose outputs
tests/test_capacity_gate_off_goldens.py freezes into
tests/fixtures/capacity_aware/goldens.json. Later gated work (the actual
capacity_aware feature) must reproduce these bit-for-bit with the gate off —
this module is the fixture side of that contract, cus.py itself is untouched.

Conventions (matches tests/test_lane_clustering_spread.py, tests/test_login_pool.py):
  - `_iso_in(minutes)`: relative-offset timestamps so goldens never drift with
    wall-clock time (see test_lane_clustering_spread.py's identical helper).
  - `_acct(five_h, seven_d, **overrides)`: plain state-account dict builder —
    the dict-level shape `state["accounts"][name]` pick_swap_target/decide_swap
    read directly (current_5h_pct/current_7d_pct/next_swap_at_pct/...). This is
    the state-side sibling of test_login_pool.py's `_usage()` builder, which
    instead builds the AccountUsage dataclass decide_swap's usage_by_account
    argument needs.
  - `usage_from_state(state)`: derives that AccountUsage-shaped usage_by_account
    straight from the SAME state["accounts"] dicts a scenario already built for
    pick_swap_target, so a scenario's numbers live in exactly one place and
    can't drift apart between the two call shapes.

Determinism: `build_scenarios()` returns a FRESH list of (name, state, config)
triples on every call — no shared mutable module-level dicts — so the
comparison test can run each scenario through both gate-off variants (no
`capacity_aware` key at all, and `{"capacity_aware": {"enabled": False}}`)
without one run's mutation leaking into the other. Every value is kept far
from decision boundaries except the burn-soon / no-reset-soon pair, which
exist specifically to pin that boundary (see their docstrings below). If a
recorded golden ever proves time-flaky, fix the scenario's margin — never
loosen the comparison test's assertion.

Run standalone: python3 tests/capacity_fixtures.py  (prints scenario names)
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# `import cus` (parent dir) — same bootstrap as test_lane_clustering_spread.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _iso_in(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _acct(five_h: float, seven_d: float, **overrides) -> dict:
    """Plain state-account dict — current_5h_pct/current_7d_pct plus whatever
    per-account fields (next_swap_at_pct, five_hour_resets_at, ...) a scenario
    needs. This is the dict pick_swap_target/decide_swap read off
    state["accounts"][name]; contrast with `_usage()`-style AccountUsage
    objects (see usage_from_state below), which is decide_swap's OTHER,
    separate input shape."""
    acct = {"current_5h_pct": five_h, "current_7d_pct": seven_d}
    acct.update(overrides)
    return acct


def usage_from_state(state: dict) -> dict[str, "cus.AccountUsage"]:
    """Derive decide_swap's usage_by_account straight from state["accounts"]
    so a scenario's numbers are defined exactly once (avoids the state dict
    and the AccountUsage dict silently drifting apart)."""
    out: dict[str, cus.AccountUsage] = {}
    for name, acct in state.get("accounts", {}).items():
        out[name] = cus.AccountUsage(
            five_hour=cus.UsageWindow(utilization=acct.get("current_5h_pct", 0.0),
                                       resets_at=acct.get("five_hour_resets_at")),
            seven_day=cus.UsageWindow(utilization=acct.get("current_7d_pct", 0.0), resets_at=None),
        )
    return out


def _cfg(strategy: str = "smart", **overrides) -> dict:
    return cus.deep_merge(cus.DEFAULT_CONFIG, {"strategy": strategy, **overrides})


def build_scenarios() -> list[tuple[str, dict, dict]]:
    """Return a FRESH list of (name, state, config) triples. Every scenario's
    state carries an "active" account so both pick_swap_target AND decide_swap
    get exercised (per the task-2 brief: "where the scenario provides an
    active account" — here, always)."""
    scenarios: list[tuple[str, dict, dict]] = []

    # ------------------------------------------------------------------
    # 1. Homogeneous fleet, IDLE: every account tied on usage, no lane
    #    occupancy tracked at all (no `_lane_load` key). Active is itself
    #    idle (10%), far under every default threshold, so decide_swap holds
    #    — this scenario is purely about pick_swap_target's tie-break
    #    (stable-sort / insertion order) over identical candidates.
    # ------------------------------------------------------------------
    scenarios.append((
        "homogeneous_idle",
        {
            "active": "active",
            "accounts": {
                "active": _acct(10.0, 10.0),
                "acct0": _acct(10.0, 10.0),
                "acct1": _acct(10.0, 10.0),
                "acct2": _acct(10.0, 10.0),
                "acct3": _acct(10.0, 10.0),
            },
        },
        _cfg("smart"),
    ))

    # ------------------------------------------------------------------
    # 2. Homogeneous fleet, LOADED: same tied-usage candidates, but
    #    `_lane_load` carries 0-3 live lanes per candidate. Active is HOT
    #    (96%, ladder-tripped) so decide_swap actually swaps, landing on
    #    whichever candidate the spread_lanes cluster_penalty (on by
    #    default) steers pick_swap_target toward — the least-loaded one.
    # ------------------------------------------------------------------
    scenarios.append((
        "homogeneous_loaded",
        {
            "active": "hot0",
            "accounts": {
                "hot0": _acct(96.0, 20.0, next_swap_at_pct=95),
                "acct0": _acct(10.0, 10.0, next_swap_at_pct=95),
                "acct1": _acct(10.0, 10.0, next_swap_at_pct=95),
                "acct2": _acct(10.0, 10.0, next_swap_at_pct=95),
                "acct3": _acct(10.0, 10.0, next_swap_at_pct=95),
            },
            "_lane_load": {"acct0": 0, "acct1": 1, "acct2": 2, "acct3": 3},
        },
        _cfg("smart"),
    ))

    # ------------------------------------------------------------------
    # 3. Heterogeneous mix, 5h variant: candidates spread across
    #    60/65/71/75/80/88/99/100% CURRENT_5H_PCT (7d held uniformly low —
    #    well under the 80% hard_7d_cap — so the 7d hard-cap filter never
    #    fires here; only the 100% never_swap_to_pct wall and the
    #    would-re-trip degraded fallback are in play). Active is a separate
    #    hot/ladder-tripped account so this scenario isolates candidate
    #    RANKING, not the trigger.
    # ------------------------------------------------------------------
    scenarios.append((
        "heterogeneous_5h_mix",
        {
            "active": "hot",
            "accounts": {
                "hot": _acct(96.0, 15.0, next_swap_at_pct=95),
                "p60": _acct(60.0, 15.0),
                "p65": _acct(65.0, 15.0),
                "p71": _acct(71.0, 15.0),
                "p75": _acct(75.0, 15.0),
                "p80": _acct(80.0, 15.0),
                "p88": _acct(88.0, 15.0),
                "p99": _acct(99.0, 15.0),
                "p100": _acct(100.0, 15.0),
            },
        },
        _cfg("smart"),
    ))

    # ------------------------------------------------------------------
    # 4. Heterogeneous mix, 7d variant: the same 8 percentages, now on
    #    CURRENT_7D_PCT (5h uniformly low). This exercises the aggregate
    #    hard_7d_cap filter (80/88/99/100 excluded before ranking even
    #    starts), unlike the 5h variant above.
    # ------------------------------------------------------------------
    scenarios.append((
        "heterogeneous_7d_mix",
        {
            "active": "hot",
            "accounts": {
                "hot": _acct(96.0, 15.0, next_swap_at_pct=95),
                "q60": _acct(15.0, 60.0),
                "q65": _acct(15.0, 65.0),
                "q71": _acct(15.0, 71.0),
                "q75": _acct(15.0, 75.0),
                "q80": _acct(15.0, 80.0),
                "q88": _acct(15.0, 88.0),
                "q99": _acct(15.0, 99.0),
                "q100": _acct(15.0, 100.0),
            },
        },
        _cfg("smart"),
    ))

    # ------------------------------------------------------------------
    # 5-9. Each strategy (smart, lowest_usage, headroom, drain,
    #    strict_priority) over the SAME small heterogeneous fleet: a hot,
    #    ladder-tripped active (96%/20%) plus three clearly-separated
    #    candidates. Values are deliberately far apart (10/20/30 5h,
    #    10/15/25 7d) so each strategy's own scoring/ordering logic is the
    #    only thing that can move the pick.
    # ------------------------------------------------------------------
    def _strategy_accounts() -> dict:
        return {
            "active": _acct(96.0, 20.0, next_swap_at_pct=95),
            "acctA": _acct(10.0, 10.0, next_swap_at_pct=95),
            "acctB": _acct(20.0, 15.0, next_swap_at_pct=95),
            "acctC": _acct(30.0, 25.0, next_swap_at_pct=95),
        }

    for strat in ("smart", "lowest_usage", "headroom", "drain"):
        scenarios.append((
            f"strategy_{strat}",
            {"active": "active", "accounts": _strategy_accounts()},
            _cfg(strat),
        ))

    # strict_priority additionally needs config.accounts[].priority (it reads
    # priority from CONFIG, not from the state dict) — gets its own dedicated
    # config so the priority order (B < A < C) actually differentiates the
    # pick from the other four strategies above, all of which converge on
    # acctA (lowest usage / highest headroom score).
    scenarios.append((
        "strategy_strict_priority",
        {"active": "active", "accounts": _strategy_accounts()},
        _cfg("strict_priority", accounts=[
            {"name": "acctB", "priority": 1},
            {"name": "acctA", "priority": 2},
            {"name": "acctC", "priority": 3},
        ]),
    ))

    # ------------------------------------------------------------------
    # 10. Ladder thresholds AT DEFAULTS: active has NO next_swap_at_pct set
    #    at all, so decide_swap's Trigger 2 falls back to
    #    thresholds.steps[0] (50) — the baseline first-rung behavior, as
    #    opposed to the strategy scenarios above which pin an ADVANCED rung
    #    (95) on the active account.
    # ------------------------------------------------------------------
    scenarios.append((
        "ladder_defaults",
        {
            "active": "active2",
            "accounts": {
                "active2": _acct(65.0, 20.0),
                "acctX": _acct(10.0, 10.0),
                "acctY": _acct(20.0, 15.0),
                "acctZ": _acct(30.0, 25.0),
            },
        },
        _cfg("smart"),
    ))

    # ------------------------------------------------------------------
    # 11. Ladder thresholds with PER-ACCOUNT next_swap_at_pct 65/70/75/90/
    #    100(sentinel), strategy=drain (the strategy whose pass-1 filter
    #    directly reads each candidate's OWN next_swap_at_pct: "effective_pct
    #    < next_swap_at_pct"). acct_65/acct_70 sit OVER their own rung (fail
    #    pass-1, excluded); acct_75/acct_90/acct_100 sit comfortably under
    #    theirs (pass), with acct_100 proving the literal sentinel value
    #    (100) is used as-is here — NOT clamped the way the universal
    #    would-re-trip filter clamps to steps[0].
    # ------------------------------------------------------------------
    scenarios.append((
        "ladder_custom_thresholds",
        {
            "active": "hot",
            "accounts": {
                "hot": _acct(96.0, 20.0, next_swap_at_pct=95),
                "acct_65": _acct(70.0, 10.0, next_swap_at_pct=65),   # over own rung -> fails pass-1
                "acct_70": _acct(72.0, 10.0, next_swap_at_pct=70),   # over own rung -> fails pass-1
                "acct_75": _acct(40.0, 10.0, next_swap_at_pct=75),   # under -> passes
                "acct_90": _acct(40.0, 10.0, next_swap_at_pct=90),   # under -> passes
                "acct_100": _acct(95.0, 10.0, next_swap_at_pct=100),  # sentinel, under -> passes
            },
        },
        _cfg("drain"),
    ))

    # ------------------------------------------------------------------
    # 12-13. Burn-soon / no-reset-soon twin (decide_swap's
    #    _maybe_burn_before_reset trigger, GH #42). Active is IDLE (5h=0%,
    #    clock not ticking -> its own "remaining" reads as infinite, so the
    #    active-resets-later guard is trivially satisfied either way).
    #      - burn_soon: target's 5h resets ~22min out (well inside the
    #        default 30min reset_window, well clear of the 0min/30min
    #        edges) -> burn_before_reset fires.
    #      - no_reset_soon: same fleet, target's 5h resets 3h out (well
    #        outside the 30min window AND outside the 2h burn_window used by
    #        the smart picker's bonus) -> burn_before_reset does NOT fire;
    #        decide_swap holds (below_threshold) and pick_swap_target picks
    #        the untargeted-but-lower-usage "spare" account instead.
    # ------------------------------------------------------------------
    # NOTE: the target's current_5h_pct must stay CLEAR of 50 — the universal
    # would-immediately-re-trip filter clamps to thresholds.steps[0] (50)
    # regardless of any per-account next_swap_at_pct, so a target sitting AT
    # that clamp gets excluded from candidates before scoring ever runs (this
    # bit a first draft: reset-soon at exactly 50% was filtered out and
    # pick_swap_target fell back to "spare", masking the burn_before_reset
    # trigger entirely). 40% keeps a comfortable margin below the clamp while
    # still leaving >=15% headroom (min_candidate_headroom_pct).
    scenarios.append((
        "burn_soon",
        {
            "active": "idle",
            "accounts": {
                "idle": _acct(0.0, 5.0),
                "reset-soon": _acct(40.0, 10.0, five_hour_resets_at=_iso_in(22)),
                "spare": _acct(5.0, 5.0),
            },
        },
        _cfg("smart"),
    ))
    scenarios.append((
        "no_reset_soon",
        {
            "active": "idle",
            "accounts": {
                "idle": _acct(0.0, 5.0),
                "reset-later": _acct(40.0, 10.0, five_hour_resets_at=_iso_in(180)),
                "spare": _acct(5.0, 5.0),
            },
        },
        _cfg("smart"),
    ))

    return scenarios


# ---------------------------------------------------------------------------
# Capacity-aware GATE-ON conversion fixtures (Task 4, capacity-aware spec
# 2026-07-10). These are NEW builders — the gate-off golden machinery above
# (build_scenarios/_acct/_cfg/usage_from_state) is deliberately untouched so
# the goldens contract stays intact. Used only by
# tests/test_capacity_gate_conversions.py.
# ---------------------------------------------------------------------------

def cap_config(strategy: str = "smart", *, reference_x: float = 5,
               cluster_penalty: float = 60, enabled: bool = True, **overrides) -> dict:
    """Config with the capacity-aware gate ON, `reference_x` pinned, and the
    formula-1 `spread_lanes.cluster_penalty` set (default 60 = the spec's
    worked-example value). `enabled=False` yields the byte-identical gate-off
    twin for A/B assertions."""
    return cus.deep_merge(cus.DEFAULT_CONFIG, {
        "strategy": strategy,
        "capacity_aware": {"enabled": enabled, "reference_x": reference_x},
        "spread_lanes": {"enabled": True, "cluster_penalty": cluster_penalty},
        **overrides,
    })


def cap_ctx(reference_x: float = 5, capacity_x_by_name: dict | None = None,
            lane_load_by_name: dict | None = None) -> dict:
    """Hand-built `_capacity_ctx`-shaped dict for stashing into
    `state['_capacity_ctx']` (the picker/decide_swap fallback reads it before
    building fresh) or passing straight to `_remaining_units` /
    `_target_would_immediately_re_trip`. Full control of ratios + lane counts
    without touching /proc or credentials files."""
    return {
        "reference_x": float(reference_x),
        "capacity_x_by_name": dict(capacity_x_by_name or {}),
        "lane_load_by_name": dict(lane_load_by_name or {}),
    }


if __name__ == "__main__":
    for name, state, config in build_scenarios():
        print(name)
