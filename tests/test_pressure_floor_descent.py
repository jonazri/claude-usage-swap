"""Task 25 (spec-2 token-pressure forecaster, STAGE 1): the critical-level
`critical_share_floor_steps` [15,10,5,2.5] descent, EXTENDING Task 24's base
single-floor `_size_reduction_walk` (design doc §5.4 "adaptive lower") --
run entirely within one critical targeting pass (G7).

`_size_reduction_walk(candidates, required, safety_factor, config, level)`:
``level == "critical"`` descends `config.pressure.critical_share_floor_steps`,
RE-FILTERING the already-annotated candidate pool at each step's lower floor
(a lower floor admits MORE, smaller-share sessions) and STOPPING the instant
`planned_shed >= required * safety_factor`; the floor actually used is
recorded on the returned plan (`floor`). ``level == "elevated"`` (the Task 24
default) never descends past the base `share_floor_pct` (15) -- unchanged
behavior, proven by `test_size_reduction_walk_direct_tiebreak` in
`tests/test_pressure_dryrun.py` staying green with no `level` kwarg at all.
Reaching `share_floor_min_pct` (2.5) still unmet -> `escalate=True`, never a
vacuous clear (§5.4). Stateless/per-episode: no module/global ratchet --
each call starts fresh at 15.

HARNESS: ``python3 -m pytest tests/ -q``.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


CFG = {"pressure": {"share_floor_pct": 15.0}}


def _candidate(session_id, share_pct_per_min, contribution, elasticity_weight=1.0,
               trend="steady", cls="subagent-heavy"):
    """A pre-annotated §5.2 candidate, shaped exactly like
    `_pressure_dry_run_candidates`'s own output (Task 25 adds
    `share_pct_per_min` to that shape so the descent can re-filter the
    already-annotated pool without re-deriving it from raw sessions)."""
    return {
        "session_id": session_id,
        "class": cls,
        "trend": trend,
        "contribution": contribution,
        "elasticity_weight": elasticity_weight,
        "share_pct_per_min": share_pct_per_min,
    }


def _session(session_id, cls, rate, account_shares, trend="steady"):
    return {
        "session_id": session_id,
        "account_shares": dict(account_shares),
        "model": None,
        "fable_share": None,
        "pane": "%1",
        "socket": "s0",
        "cwd": "/x",
        "class": cls,
        "rate": rate,
        "trend": trend,
        "coordinator_of": None,
    }


def _acct(capacity_x, required_5h=0.0):
    return {
        "capacity_x": capacity_x,
        "5h": {"pct": 50.0, "gate": 94.0, "remaining_units": 1.0,
               "burn_pct_per_min": 0.0, "pinned_eta_min": None,
               "required_reduction_pct_per_min": required_5h},
        "7d": {"pct": 10.0, "gate": 80.0, "remaining_units": 1.0,
               "burn_pct_per_min": 0.0, "pinned_eta_min": None,
               "required_reduction_pct_per_min": 0.0},
        "fable_weekly": {"pct": 10.0, "gate": 95.0, "level_bound": True},
    }


def _pool_block():
    return {
        "5h": {"capacity_units": 10.0, "remaining_units": 1.0,
               "burn_units_per_min": 0.0, "exhaustion_eta_min": 120.0,
               "required_reduction_units_per_min": 0.0,
               "release_suppressed": False},
        "7d": {"capacity_units": 10.0, "remaining_units": 1.0,
               "burn_units_per_min": 0.0, "exhaustion_eta_min": None,
               "required_reduction_units_per_min": 0.0,
               "release_suppressed": False},
    }


def _account_state(sessions, required, safety_factor, name="acctX",
                    capacity_x=10, level="elevated"):
    return {
        "level": level,
        "reference_x": 5.0,
        "safety_factor": safety_factor,
        "binding": {"view": "account", "name": name, "constraint": "5h",
                    "window": "5h", "eta_min": 60.0},
        "pool": _pool_block(),
        "accounts": {name: _acct(capacity_x, required_5h=required)},
        "sessions": sessions,
    }


# ===================== direct `_size_reduction_walk` interface =====================

def test_critical_descends_to_meeting_floor():
    """Unmeetable at the base 15% floor, and still unmeetable at 10%, but
    meetable once the floor descends to 5% (a session excluded at 15%/10%
    with share%/min=6.0 becomes eligible at 5%) -- descends [15, 10, 5],
    STOPS the instant it meets (never reaches 2.5), `met=True`, and the
    floor actually used (5) is recorded on the plan."""
    candidates = [
        _candidate("big15", share_pct_per_min=20.0, contribution=3.0),
        _candidate("mid10", share_pct_per_min=12.0, contribution=3.0),
        _candidate("low5", share_pct_per_min=6.0, contribution=5.0),
    ]
    # threshold = 10.0 * 1.0 = 10.0
    #   floor=15: only big15 (20>=15) -> shed 3.0  < 10.0 -> not met
    #   floor=10: big15+mid10 (20,12>=10) -> shed 6.0  < 10.0 -> not met
    #   floor=5:  all three (20,12,6>=5) -> shed >= 10.0 -> met
    plan = cus._size_reduction_walk(candidates, required=10.0, safety_factor=1.0,
                                     config=CFG, level="critical")
    assert plan["met"] is True
    assert plan["escalate"] is False
    assert plan["reason"] is None
    assert plan["floor"] == pytest.approx(5.0)
    assert plan["planned_shed"] >= 10.0
    assert "low5" in [t["session_id"] for t in plan["targets"]]


def test_critical_unmeetable_even_at_min_floor_escalates():
    """A candidate that only ever clears the deepest floor (2.5) and still
    can't shed enough -- descends all the way to `share_floor_min_pct`
    (2.5) and, still unmet, escalates rather than vacuously clearing."""
    candidates = [
        _candidate("tiny", share_pct_per_min=3.0, contribution=1.0),
    ]
    # threshold = 10.0; tiny only admitted at floor=2.5 (3.0>=2.5), shed 1.0 < 10.0.
    plan = cus._size_reduction_walk(candidates, required=10.0, safety_factor=1.0,
                                     config=CFG, level="critical")
    assert plan["met"] is False
    assert plan["escalate"] is True
    assert plan["floor"] == pytest.approx(2.5)
    assert isinstance(plan["reason"], str) and plan["reason"]


def test_elevated_never_descends_and_escalates_direct():
    """`level == "elevated"` (Task 24's default) never descends below the
    base floor even when called directly with a pool that WOULD meet at a
    lower floor step -- proving the descent is critical-only. (The
    candidates here are handed in exactly as Task 24 always assumed:
    already filtered by the caller at the base floor, so `mid10` below is
    simply never in this list in real elevated usage -- see
    `test_elevated_never_descends_and_escalates` below for the full
    `dry_run_target` proof that a low-share session never even reaches
    this function on the elevated path.)"""
    candidates = [
        _candidate("big15", share_pct_per_min=20.0, contribution=3.0),
    ]
    plan = cus._size_reduction_walk(candidates, required=10.0, safety_factor=1.0,
                                     config=CFG, level="elevated")
    assert plan["met"] is False
    assert plan["escalate"] is True
    assert plan["floor"] == pytest.approx(15.0)


def test_two_episodes_no_ratchet():
    """Two independent calls in sequence: the first needs a deep descent
    to 5% to meet; the second (fresh candidates, already meetable at the
    base 15%) must start over at 15% -- proving there is no module/global
    ratchet state carried from episode 1's ending floor into episode 2. A
    buggy "resume where we left off" implementation would still report
    `met=True` here (5% is more permissive than 15%) but would wrongly
    record `floor=5.0` instead of the correct shallowest-sufficient 15.0."""
    deep_candidates = [_candidate("deep", share_pct_per_min=6.0, contribution=8.0)]
    plan1 = cus._size_reduction_walk(deep_candidates, required=5.0, safety_factor=1.0,
                                      config=CFG, level="critical")
    assert plan1["met"] is True
    assert plan1["floor"] == pytest.approx(5.0)

    shallow_candidates = [_candidate("shallow", share_pct_per_min=20.0, contribution=8.0)]
    plan2 = cus._size_reduction_walk(shallow_candidates, required=5.0, safety_factor=1.0,
                                      config=CFG, level="critical")
    assert plan2["met"] is True
    assert plan2["floor"] == pytest.approx(15.0), (
        "second episode must start fresh at 15% -- a ratchet bug would "
        "resume from episode 1's ending floor (5%) instead"
    )


# ===================== end-to-end via `dry_run_target` =====================

def test_elevated_never_descends_and_escalates():
    """End-to-end proof (not just the direct-call one above): an
    elevated-level breach whose only elastic candidate sits below
    `share_floor_pct`=15 (12.0 -- would clear a critical-descent floor of
    10 or 5) must never even become a candidate on the elevated path --
    `dry_run_target` escalates exactly as Task 24's base single-floor walk
    did, never touching the descent."""
    low = _session("low", "workflow", rate=12.0, trend="steady",
                   account_shares={"acctE": 1.0})
    state = _account_state([low], required=5.0, safety_factor=1.0,
                            name="acctE", capacity_x=10, level="elevated")
    plan = cus.dry_run_target(state, CFG)

    assert plan["met"] is False
    assert plan["escalate"] is True
    assert plan["targets"] == []


def test_interactive_never_admitted_at_any_floor():
    """The class gate (never `interactive`, never `idle`) sits upstream of
    the floor in `_pressure_dry_run_candidates` -- a critical-level
    descent all the way to 2.5% must still never admit a human session, no
    matter how enormous its share%/min is."""
    human = _session("human", "interactive", rate=999.0, trend="steady",
                      account_shares={"acctC": 1.0})
    small = _session("small", "workflow", rate=3.0, trend="steady",
                      account_shares={"acctC": 1.0})  # share%/min=3.0
    state = _account_state([human, small], required=2.0, safety_factor=1.0,
                            name="acctC", capacity_x=10, level="critical")
    plan = cus.dry_run_target(state, CFG)

    target_ids = [t["session_id"] for t in plan["targets"]]
    assert "human" not in target_ids
    assert target_ids == ["small"]
    assert plan["met"] is True
    assert plan["escalate"] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
