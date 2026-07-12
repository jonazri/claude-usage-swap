"""Capacity-aware GATE-ON conversion tests (Task 4, capacity-aware spec
2026-07-10, docs/plans/2026-07-10-capacity-aware-anti-herding.md — rows G1, G2,
G4, G5, G6, G7, G9-Trigger-1 + "The formula changes").

Each test drives the REAL cus.py functions with the gate ON and asserts the
reference-unit behavior, and (where a divergence is the point) pairs it against
the byte-identical gate-off twin. The gate-off goldens contract itself lives in
tests/test_capacity_gate_off_goldens.py; here we prove the NEW gated branches.

ctx is supplied by stashing a hand-built `_capacity_ctx`-shaped dict into
`state['_capacity_ctx']` (the deliberate fallback resolution: pick_swap_target /
decide_swap read a stash before building fresh), so ratios + lane counts are
fully controlled without touching /proc or credentials files.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cus  # noqa: E402
from capacity_fixtures import _acct, _iso_in, cap_config, cap_ctx, usage_from_state  # noqa: E402

STEPS0 = 50  # thresholds.steps default is [50, 75, 90]


def _decide(state, config, trace):
    return cus.decide_swap(state, config, usage_from_state(state), trace)


# ==========================================================================
# G1 — the reference-unit ranking key (_capacity_rank_key), formula 1.
# ==========================================================================

def test_g1_worked_triple_exact():
    """Spec worked example (reference_x=5, cluster_penalty=60): the three keys
    are exactly 0.40 / 0.10 / 1.40, and the 20x-idle out-keys both."""
    cfg = cap_config(cluster_penalty=60)
    # idle 5x @ 60% (lowest_usage score_pts = 100 - 60 = 40), 0 lanes.
    k_5x = cus._capacity_rank_key(40, "a", cap_ctx(5, {"a": 5}, {}), cfg)
    # 20x @ 65% (score_pts 35), 1 lane.
    k_20x_1lane = cus._capacity_rank_key(35, "b", cap_ctx(5, {"b": 20}, {"b": 1}), cfg)
    # same 20x @ 65% idle (0 lanes).
    k_20x_idle = cus._capacity_rank_key(35, "b", cap_ctx(5, {"b": 20}, {}), cfg)

    assert abs(k_5x - 0.40) < 1e-9
    assert abs(k_20x_1lane - 0.10) < 1e-9
    assert abs(k_20x_idle - 1.40) < 1e-9
    # ordering: idle-20x wins, then 5x, then the loaded 20x.
    assert k_20x_idle > k_5x > k_20x_1lane


def test_g1_smart_bonus_active_magnet_crossover():
    """A bonus-active smart magnet (score ~140.4 on a 20x) out-keys an idle
    5x@60 (0.40) until it accretes its THIRD lane, where it finally loses.
    Assert orderings exactly; key values approximately (tol 0.05)."""
    cfg = cap_config(cluster_penalty=60)
    score_magnet = 140.4
    k_idle = cus._capacity_rank_key(score_magnet, "m", cap_ctx(5, {"m": 20}, {}), cfg)
    k_2 = cus._capacity_rank_key(score_magnet, "m", cap_ctx(5, {"m": 20}, {"m": 2}), cfg)
    k_3 = cus._capacity_rank_key(score_magnet, "m", cap_ctx(5, {"m": 20}, {"m": 3}), cfg)
    k_spare = cus._capacity_rank_key(40, "i", cap_ctx(5, {"i": 5}, {}), cfg)  # idle 5x@60

    assert abs(k_idle - 5.62) < 0.05
    assert abs(k_2 - 0.67) < 0.05
    assert abs(k_3 - (-0.40)) < 0.05
    assert abs(k_spare - 0.40) < 1e-9
    # magnet dominates until its 3rd lane, then loses to the idle 5x.
    assert k_idle > k_2 > k_spare > k_3


# ==========================================================================
# G2 — _target_would_immediately_re_trip, formula 2 (health line ÷(lanes+1)).
# ==========================================================================

def test_g2_health_boundary_pct_equals_steps0_not_healthy():
    """pct == steps[0] at ratio 1 / 0 lanes sits exactly ON the line; strict
    `>` means NOT healthy ⇒ would re-trip (True)."""
    cfg = cap_config()
    ctx = cap_ctx(5, {"a": 5}, {})  # ratio 1
    acct = _acct(float(STEPS0), 10.0)
    assert cus._target_would_immediately_re_trip(acct, cfg, name="a", ctx=ctx) is True
    # just under the line is healthy.
    assert cus._target_would_immediately_re_trip(_acct(STEPS0 - 0.1, 10.0), cfg, name="a", ctx=ctx) is False


def test_g2_trip_equality_trips_and_big_tier_gets_runway():
    """A 20x sitting exactly at remaining==(100-steps0)/100 trips (equality is
    a trip); a hair under is healthy. And a 20x@80 (0.8u) is healthy where a
    ratio-1 account at 80 would be full."""
    cfg = cap_config()
    ctx20 = cap_ctx(5, {"b": 20}, {})  # ratio 4
    # remaining = (100-pct)/100*4 == 0.5  ⇒  pct == 87.5  (equality ⇒ trips)
    assert cus._target_would_immediately_re_trip(_acct(87.5, 10.0), cfg, name="b", ctx=ctx20) is True
    assert cus._target_would_immediately_re_trip(_acct(87.4, 10.0), cfg, name="b", ctx=ctx20) is False
    # 20x@80 has 0.8u/lane > 0.5u line ⇒ healthy (not full) — the anti-herding win.
    assert cus._target_would_immediately_re_trip(_acct(80.0, 10.0), cfg, name="b", ctx=ctx20) is False


def test_g2_lane_divisor_shrinks_per_lane_headroom():
    """The ÷(lanes+1) contention divisor: a 20x@80 healthy at 0 lanes goes
    unhealthy once 3 lanes already share it (0.8/4 = 0.2 < 0.5)."""
    cfg = cap_config()
    acct = _acct(80.0, 10.0)
    assert cus._target_would_immediately_re_trip(acct, cfg, name="b", ctx=cap_ctx(5, {"b": 20}, {})) is False
    assert cus._target_would_immediately_re_trip(acct, cfg, name="b", ctx=cap_ctx(5, {"b": 20}, {"b": 3})) is True


def test_g2_ctx_none_is_percent_path_unchanged():
    """Gate on but no name/ctx ⇒ today's raw-percent path (>= steps[0])."""
    cfg = cap_config()
    assert cus._target_would_immediately_re_trip(_acct(80.0, 10.0), cfg) is True   # 80 >= 50
    assert cus._target_would_immediately_re_trip(_acct(40.0, 10.0), cfg) is False  # 40 < 50


# ==========================================================================
# G4 — decide_swap Trigger 2 (progressive ladder), reference units.
# ==========================================================================

def test_g4_ladder_20x_at_80_next70_holds_trips_at_92_5():
    """20x active, next_swap_at=70: at 80% it is ACCEPTED (not tripped — holds
    below the unit line), and only trips at >= 92.5% (remaining 0.3u)."""
    cfg = cap_config(strategy="smart")
    ctx = cap_ctx(5, {"big": 20, "fresh": 5}, {})

    def run(pct):
        state = {
            "active": "big",
            "_capacity_ctx": ctx,
            "accounts": {
                "big": _acct(pct, 10.0, next_swap_at_pct=70),
                "fresh": _acct(5.0, 5.0),
            },
        }
        trace = {}
        _decide(state, cfg, trace)
        return trace.get("gate")

    assert run(80.0) == "below_threshold"    # accepted, not tripped
    assert run(92.4) == "below_threshold"    # still holds just under 92.5
    assert run(92.6) != "below_threshold"    # trips (ladder proceeds)


def test_g4_reduces_to_percent_at_ratio_one():
    """A ratio-1 active with next_swap_at=70 still trips exactly at 70% (unit
    line reduces to the raw percent form)."""
    cfg = cap_config(strategy="smart")
    ctx = cap_ctx(5, {"one": 5, "fresh": 5}, {})

    def gate(pct):
        state = {"active": "one", "_capacity_ctx": ctx,
                 "accounts": {"one": _acct(pct, 10.0, next_swap_at_pct=70),
                              "fresh": _acct(5.0, 5.0)}}
        trace = {}
        _decide(state, cfg, trace)
        return trace.get("gate")

    assert gate(69.9) == "below_threshold"
    assert gate(70.0) != "below_threshold"


# ==========================================================================
# G9 (Trigger-1 arm) — aggregate 7d hard cap, reference units.
# ==========================================================================

def test_g9_aggregate_7d_trigger1_trips_at_95_for_20x():
    """20x active: the aggregate 7d cap (default 80) does NOT force at 7d=88
    (0.48u > 0.2u) but DOES at 7d=95 (0.2u == the line)."""
    cfg = cap_config(strategy="smart")
    ctx = cap_ctx(5, {"big": 20, "fresh": 5}, {})

    def gate(seven_d):
        state = {"active": "big", "_capacity_ctx": ctx,
                 "accounts": {"big": _acct(10.0, seven_d, next_swap_at_pct=50),
                              "fresh": _acct(5.0, 5.0)}}
        trace = {}
        _decide(state, cfg, trace)
        return trace.get("gate")

    assert gate(88.0) != "hard_7d_cap"   # aggregate cap not forced
    assert gate(95.0) == "hard_7d_cap"   # trips at 95 (remaining 0.2u)


def test_g9_picker_safe_7d_admits_idle_20x_at_88():
    """The picker's safe_7d filter converts the same way: an idle 20x@7d=88 is
    ADMITTED (0.48u > 0.2u) gate-on where gate-off would degrade past the 80%
    cap; a 5x@7d=99 stays excluded."""
    state = {
        "active": "hot",
        "_capacity_ctx": cap_ctx(5, {"hot": 5, "big20x": 20, "capped5x": 5}, {}),
        "accounts": {
            "hot": _acct(96.0, 20.0, next_swap_at_pct=95),
            "big20x": _acct(5.0, 88.0),
            "capped5x": _acct(5.0, 99.0),
        },
    }
    on = cus.pick_swap_target(state, cap_config(strategy="smart"))
    assert on is not None and on.name == "big20x"
    assert "no targets below 7d cap" not in on.reason  # admitted cleanly, not degraded

    off_state = dict(state)
    off_state.pop("_capacity_ctx", None)
    off = cus.pick_swap_target(off_state, cap_config(strategy="smart", enabled=False))
    assert "no targets below 7d cap" in off.reason  # gate-off: both over the cap → degraded


# ==========================================================================
# G6 — bbr Trigger-2.5 precheck, reference units (RAW current_max_pct source).
# ==========================================================================

def test_g6_20x_at_80_stays_bbr_eligible():
    """20x active @80% with next_swap_at=70: gate-on the bbr precheck is
    eligible (0.8u > 0.3u) so burn-before-reset FIRES onto a soon-resetting
    target; gate-off the raw 80% >= 70% skips bbr and the ladder handles it."""
    ctx = cap_ctx(5, {"big": 20, "burn": 5}, {})
    state = {
        "active": "big",
        "_capacity_ctx": ctx,
        "accounts": {
            # active 5h clock not ticking (no resets_at) ⇒ far-from-reset guard ok.
            "big": _acct(80.0, 10.0, next_swap_at_pct=70),
            # burn target: >=15% headroom, 5h resets ~22min out (inside 30min window).
            "burn": _acct(40.0, 10.0, five_hour_resets_at=_iso_in(22)),
        },
    }
    trace = {}
    _decide(state, cap_config(strategy="smart"), trace)
    assert trace.get("gate") == "burn_before_reset"

    off_state = {"active": "big",
                 "accounts": {k: dict(v) for k, v in state["accounts"].items()}}
    trace_off = {}
    _decide(off_state, cap_config(strategy="smart", enabled=False), trace_off)
    assert trace_off.get("gate") != "burn_before_reset"


# ==========================================================================
# G5 — min_improvement_gate ("must improve"), reference units.
# ==========================================================================

def test_g5_hysteresis_5x71_to_idle_20x75_proceeds_gate_on():
    """active 5x@71% (0.29u) → target 20x@75% (1.0u): a real +0.71u
    improvement, so gate-on PROCEEDS; gate-off holds because 75% > 71%-3pp."""
    ctx = cap_ctx(5, {"small": 5, "big": 20}, {})
    base_accounts = {
        "small": _acct(71.0, 10.0, next_swap_at_pct=50),
        "big": _acct(75.0, 10.0),
    }
    state_on = {"active": "small", "_capacity_ctx": ctx,
                "accounts": {k: dict(v) for k, v in base_accounts.items()}}
    trace_on = {}
    _decide(state_on, cap_config(strategy="smart"), trace_on)
    assert trace_on.get("gate") == "ladder"          # proceeds (swaps)
    assert trace_on.get("action") == "swap"

    state_off = {"active": "small",
                 "accounts": {k: dict(v) for k, v in base_accounts.items()}}
    trace_off = {}
    _decide(state_off, cap_config(strategy="smart", enabled=False), trace_off)
    assert trace_off.get("gate") == "min_improvement_gate"  # held gate-off
    assert trace_off.get("action") == "hold"


# ==========================================================================
# G7 — drain (accept + deplete-first ordering) and strict_priority (accept).
# ==========================================================================

def test_g7_drain_orders_by_fewest_remaining_7d_units():
    """5x@7d=60 (0.4u) DEPLETES BEFORE 20x@7d=75 (1.0u) gate-on (fewest
    remaining-7d-units first), reversing gate-off's closest-to-raw-cap order.
    (Both kept under the 80% aggregate cap so the A/B set is identical — the
    spec's illustrative 85% would be excluded gate-off by that cap.)"""
    accounts = {
        "hot": _acct(96.0, 20.0, next_swap_at_pct=95),
        "small5x": _acct(10.0, 60.0, next_swap_at_pct=95),
        "big20x": _acct(10.0, 75.0, next_swap_at_pct=95),
    }
    ctx = cap_ctx(5, {"hot": 5, "small5x": 5, "big20x": 20}, {})
    state_on = {"active": "hot", "_capacity_ctx": ctx,
                "accounts": {k: dict(v) for k, v in accounts.items()}}
    on = cus.pick_swap_target(state_on, cap_config(strategy="drain"))
    assert on.name == "small5x"   # 0.4u drains before 0.6u

    state_off = {"active": "hot", "accounts": {k: dict(v) for k, v in accounts.items()}}
    off = cus.pick_swap_target(state_off, cap_config(strategy="drain", enabled=False))
    assert off.name == "big20x"   # raw 85% > 60% ⇒ closest-to-cap first


def test_g7_drain_accept_admits_20x_over_its_raw_rung():
    """Accept converts to the unit line: a 20x@7d=85 with next_swap_at=50 is
    admitted to pass-1 gate-on (0.6u > 0.5u) and chosen; gate-off it is over
    its raw rung and the pick falls to the other account via pass-2."""
    accounts = {
        "hot": _acct(96.0, 20.0, next_swap_at_pct=95),
        "big20x": _acct(10.0, 85.0, next_swap_at_pct=50),
        "other5x": _acct(10.0, 88.0, next_swap_at_pct=50),
    }
    ctx = cap_ctx(5, {"hot": 5, "big20x": 20, "other5x": 5}, {})
    state_on = {"active": "hot", "_capacity_ctx": ctx,
                "accounts": {k: dict(v) for k, v in accounts.items()}}
    on = cus.pick_swap_target(state_on, cap_config(strategy="drain"))
    assert on.name == "big20x"

    state_off = {"active": "hot", "accounts": {k: dict(v) for k, v in accounts.items()}}
    off = cus.pick_swap_target(state_off, cap_config(strategy="drain", enabled=False))
    assert off.name == "other5x"


def test_g7_strict_priority_accept_converts():
    """strict_priority converts the per-account ACCEPT to units: a lone 20x@75
    (1.0u > 0.5u) IS accepted gate-on though 75% >= its 50% rung, so the picker
    returns it — where gate-off (75% not < 50%) accepts nobody and returns
    None. (Sole candidate so the shared with_headroom fallback keeps it in both
    modes, isolating the accept-line conversion.)"""
    cfg_accounts = [{"name": "p1_20x", "priority": 1}]
    accounts = {
        "hot": _acct(96.0, 20.0, next_swap_at_pct=95),
        "p1_20x": _acct(75.0, 10.0, next_swap_at_pct=50),
    }
    ctx = cap_ctx(5, {"hot": 5, "p1_20x": 20}, {})
    state_on = {"active": "hot", "_capacity_ctx": ctx,
                "accounts": {k: dict(v) for k, v in accounts.items()}}
    on = cus.pick_swap_target(state_on, cap_config(strategy="strict_priority", accounts=cfg_accounts))
    assert on is not None and on.name == "p1_20x"   # accepted on units despite raw 75% >= 50%

    state_off = {"active": "hot", "accounts": {k: dict(v) for k, v in accounts.items()}}
    off = cus.pick_swap_target(state_off, cap_config(strategy="strict_priority", accounts=cfg_accounts, enabled=False))
    assert off is None   # 75% not < 50% rung ⇒ no priority account accepted


# ==========================================================================
# Invariant fixtures: gate-on == gate-off at reference / zero lane-load, and
# the contention division reorders where the linear penalty would not.
# ==========================================================================

def test_homogeneous_at_reference_zero_lane_load_matches_gate_off():
    """Homogeneous fleet at the reference size (all ratio 1) with no lane load:
    gate-on ranking reduces to today's, so the picked account is identical to
    the gate-off result for every scoring strategy."""
    accounts = {
        "active": _acct(96.0, 20.0, next_swap_at_pct=95),
        "c0": _acct(10.0, 12.0),
        "c1": _acct(20.0, 14.0),
        "c2": _acct(30.0, 16.0),
    }
    caps = {n: 5 for n in accounts}  # all == reference_x ⇒ ratio 1
    for strat in ("lowest_usage", "headroom", "smart"):
        off = cus.pick_swap_target(
            {"active": "active", "accounts": {k: dict(v) for k, v in accounts.items()}},
            cap_config(strategy=strat, enabled=False))
        on = cus.pick_swap_target(
            {"active": "active", "_capacity_ctx": cap_ctx(5, caps, {}),
             "accounts": {k: dict(v) for k, v in accounts.items()}},
            cap_config(strategy=strat))
        assert on.name == off.name, f"{strat}: gate-on {on.name} != gate-off {off.name}"


def test_loaded_homogeneous_contention_division_reorders():
    """Homogeneous fleet (all ratio 1) but lane-loaded: the ÷(lanes+1) division
    halves a high-score 1-lane account below a lower-score 0-lane one, a flip
    the LINEAR cluster penalty (cp=30) does not produce — so gate-on and
    gate-off pick DIFFERENT accounts."""
    cfg_on = cap_config(strategy="lowest_usage", cluster_penalty=30)
    accounts = {
        "hot": _acct(96.0, 20.0, next_swap_at_pct=95),
        "A": _acct(8.0, 5.0),    # score 92, will carry 1 lane
        "B": _acct(40.0, 5.0),   # score 60, 0 lanes
    }
    # gate-off uses state['_lane_load']; gate-on uses ctx.lane_load_by_name.
    state_off = {"active": "hot", "_lane_load": {"A": 1},
                 "accounts": {k: dict(v) for k, v in accounts.items()}}
    off = cus.pick_swap_target(state_off, cap_config(strategy="lowest_usage", cluster_penalty=30, enabled=False))
    # linear: A eff 8+30=38 < B 40 ⇒ picks A.
    assert off.name == "A"

    state_on = {"active": "hot", "_capacity_ctx": cap_ctx(5, {"hot": 5, "A": 5, "B": 5}, {"A": 1}),
                "accounts": {k: dict(v) for k, v in accounts.items()}}
    on = cus.pick_swap_target(state_on, cfg_on)
    # division: key A = 0.92/2 - 0.30 = 0.16 < key B = 0.60 ⇒ picks B.
    assert on.name == "B"


# ==========================================================================
# Burn-rate divergence: G4 (ladder) reads its own current_max_pct source while
# G2 (would-re-trip) reads the burn-EXTRAPOLATED effective pct.
# ==========================================================================

def test_burn_rate_divergence_g4_raw_vs_g2_extrapolated():
    """One ratio-1 account polled at 40% but burning toward ~55% extrapolated
    (estimator on, poll_accel off). G2 (would-re-trip) sees the extrapolated
    55% ⇒ NOT healthy; G4 (decide_swap ladder) sees the raw 40% ⇒ holds below
    the 50% rung. Same account, two pct sources — proves each site keeps its
    own expression."""
    cfg = cap_config(strategy="smart", poll_accel={"enabled": False})
    ctx = cap_ctx(5, {"burner": 5, "fresh": 5}, {})
    # burn_rate 1.5%/min, last observed 20min ago ⇒ dt capped at 10min ⇒ +15 ⇒ 55%.
    acct = _acct(40.0, 10.0, next_swap_at_pct=50,
                 burn_rate_5h_pct_per_min=1.5, last_observed_ts=_iso_in(-20))

    # G2 on the burner as a TARGET: extrapolated 55% ⇒ remaining 0.45u < 0.5u ⇒ full.
    assert cus._account_estimated_effective_pct(acct, cfg) > 50  # sanity: extrapolation crossed the rung
    assert cus._target_would_immediately_re_trip(acct, cfg, name="burner", ctx=ctx) is True

    # G4 with the same account ACTIVE: ladder reads raw 40% ⇒ holds below rung.
    state = {"active": "burner", "_capacity_ctx": ctx,
             "accounts": {"burner": dict(acct), "fresh": _acct(5.0, 5.0)}}
    trace = {}
    _decide(state, cfg, trace)
    assert trace.get("gate") == "below_threshold"
