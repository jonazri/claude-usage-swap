"""Task 24 (spec-2 token-pressure forecaster, STAGE 1): deterministic dry-run
targeting pass -- design doc §5.2 candidate walk + §5.3 sizing bridge,
reproduced zero-token/deterministically in cus (`dry_run_target` /
`_size_reduction_walk`) so the eventual shadow log can record the
"would-have-asked" plan before the flip (G7).

`dry_run_target(pressure_state, config) -> plan` consumes an already-
published Task-20 `_pressure_snapshot`-shaped dict; these fixtures are
hand-built to that exact schema (see `tests/test_pressure_json.py`'s
`test_schema_shape` for the pinned key list) rather than routed through the
full Phase-D pipeline, so each scenario's expected numbers are exact and
independently hand-computed in the comments below.

HARNESS: ``python3 -m pytest tests/ -q``.
"""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


CFG = {"pressure": {"share_floor_pct": 15.0}}


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


def _pool_block(required_5h=0.0):
    return {
        "5h": {"capacity_units": 10.0, "remaining_units": 1.0,
               "burn_units_per_min": 0.0, "exhaustion_eta_min": 120.0,
               "required_reduction_units_per_min": required_5h,
               "release_suppressed": False},
        "7d": {"capacity_units": 10.0, "remaining_units": 1.0,
               "burn_units_per_min": 0.0, "exhaustion_eta_min": None,
               "required_reduction_units_per_min": 0.0,
               "release_suppressed": False},
    }


def _pool_state(sessions, required=4.0, safety_factor=1.2):
    """Pool-bound breach snapshot: 6 mixed-tier pooled accounts
    (acc1/acc2 = 20x -> ratio 4.0; acc3..acc6 = 5x -> ratio 1.0,
    reference_x = 5.0) so a rotated session's per-account terms are NOT
    uniform (proves the ratio-aware summation, not just an equal split)."""
    accounts = {
        "acc1": _acct(20), "acc2": _acct(20),
        "acc3": _acct(5), "acc4": _acct(5), "acc5": _acct(5), "acc6": _acct(5),
    }
    return {
        "level": "critical",
        "reference_x": 5.0,
        "safety_factor": safety_factor,
        "binding": {"view": "pool", "name": "pool:5h", "constraint": "5h",
                    "window": "5h", "eta_min": 90.0},
        "pool": _pool_block(required_5h=required),
        "accounts": accounts,
        "sessions": sessions,
    }


def _account_state(sessions, required=40.0, safety_factor=1.5, name="acctX",
                    capacity_x=10):
    return {
        "level": "elevated",
        "reference_x": 5.0,
        "safety_factor": safety_factor,
        "binding": {"view": "account", "name": name, "constraint": "5h",
                    "window": "5h", "eta_min": 60.0},
        "pool": _pool_block(),
        "accounts": {name: _acct(capacity_x, required_5h=required)},
        "sessions": sessions,
    }


# ============================ (a)+(c) pool breach ============================

def test_pool_breach_meetable_excludes_interactive_sums_rotated_session():
    """§5.2/§5.3 combined: a session rotated across 6 mixed-tier accounts
    contributes the SUM of its per-account rotatable terms (FACT #5); an
    `interactive` top burner (contribution would dwarf everyone else's) and
    an `idle` session are never candidates at all, regardless of rate.

    Hand-computed contributions (Σ_acct (rate*share/100)*capacity_x/reference_x):
      rot1: acc1 100*.3/100*4=1.2 + acc2 100*.2/100*4=0.8 + acc3 100*.2/100*1=0.2
            + acc4 100*.1/100*1=0.1 + acc5 0.1 + acc6 0.1  = 2.5
      wf1:  45*1.0/100*4 = 1.8
      cl1:  100*1.0/100*1 = 1.0
    Ranked by contribution x elasticity_weight (subagent-heavy=1.0 >
    workflow=0.9 > committee-loop=0.8): rot1(2.5) > wf1(1.62) > cl1(0.8).
    threshold = 4.0 * 1.2 = 4.8; walk: 2.5 -> 4.3 -> 5.3 (>=4.8, stop).
    """
    rot1 = _session("rot1", "subagent-heavy", rate=100.0, trend="rising",
                     account_shares={"acc1": 0.3, "acc2": 0.2, "acc3": 0.2,
                                     "acc4": 0.1, "acc5": 0.1, "acc6": 0.1})
    wf1 = _session("wf1", "workflow", rate=45.0, trend="rising",
                    account_shares={"acc1": 1.0})
    cl1 = _session("cl1", "committee-loop", rate=100.0, trend="rising",
                    account_shares={"acc4": 1.0})
    int1 = _session("int1", "interactive", rate=500.0, trend="rising",
                     account_shares={"acc1": 1.0})
    idle1 = _session("idle1", "idle", rate=0.0, trend="steady",
                      account_shares={})

    state = _pool_state([rot1, wf1, cl1, int1, idle1], required=4.0, safety_factor=1.2)
    plan = cus.dry_run_target(state, CFG)

    target_ids = [t["session_id"] for t in plan["targets"]]
    assert "int1" not in target_ids, "interactive session must NEVER be targeted"
    assert "idle1" not in target_ids

    assert plan["met"] is True
    assert plan["escalate"] is False
    assert plan["reason"] is None
    assert plan["required"] == pytest.approx(4.0)
    assert plan["safety_factor"] == pytest.approx(1.2)
    assert plan["planned_shed"] >= plan["required"] * plan["safety_factor"]
    assert plan["planned_shed"] == pytest.approx(5.3, abs=1e-9)

    rot1_target = next(t for t in plan["targets"] if t["session_id"] == "rot1")
    assert rot1_target["contribution"] == pytest.approx(2.5, abs=1e-9)

    assert target_ids == ["rot1", "wf1", "cl1"]


def test_pool_breach_no_elastic_candidates_at_all_escalates():
    """§5.4's own named "dangerous case": a real pool breach whose only
    burner is `interactive` has an EMPTY eligible candidate set -- this
    must escalate, never silently look "cleared" with an empty plan."""
    int4 = _session("int4", "interactive", rate=999.0, trend="rising",
                     account_shares={"acc1": 1.0})
    state = _pool_state([int4], required=4.0, safety_factor=1.2)
    plan = cus.dry_run_target(state, CFG)

    assert plan["targets"] == []
    assert plan["met"] is False
    assert plan["escalate"] is True
    assert isinstance(plan["reason"], str) and plan["reason"]


def test_pool_breach_floor_gates_on_pct_share_not_reference_unit_contribution():
    """Follow-up 1, Part 1 (Important bug fix): the §5.2 candidacy floor
    must gate on a PERCENT-scale share%/min quantity, NOT the reference-
    unit `contribution` that §5.3 sizing uses -- for a POOL binding the two
    are on wildly different scales (contribution is `capacity_x/reference_x`
    normalized, routinely 12-100x smaller than a %-scale share).

    `steady1` (workflow, elastic) rotates 100% of its burn onto a single
    5x-tier account (ratio 1.0) at rate=30.0, trend="steady" (NOT rising,
    and no other rising sibling in this fixture): its share%/min = rate *
    account_shares["acc3"] = 30.0 * 1.0 = 30.0 (>= share_floor_pct=15 ->
    clears the floor), while its POOL-view reference-unit contribution =
    `_pressure_burn_units(30.0, ratio=1.0)` = 30.0/100*1.0 = 0.3 (far below
    15). Pre-fix, comparing 0.3 against the 15.0 floor wrongly excluded
    this session -- it would only ever have been included by accident, if
    it happened to be "rising". This proves the floor now uses share%/min:
    the session is a candidate purely because 30.0 >= 15.0, not because of
    `trend`.
    """
    steady1 = _session("steady1", "workflow", rate=30.0, trend="steady",
                        account_shares={"acc3": 1.0})
    state = _pool_state([steady1], required=4.0, safety_factor=1.2)

    candidates = cus._pressure_dry_run_candidates(
        state["sessions"], state["binding"], state["accounts"],
        state["reference_x"], CFG,
    )

    ids = [c["session_id"] for c in candidates]
    assert "steady1" in ids, (
        "a non-rising session with share%/min=30.0 (>= floor 15.0) must "
        "clear the §5.2 floor even though its pool-view reference-unit "
        "contribution (0.3) is far below share_floor_pct=15 -- the floor "
        "must compare against %-scale share, not reference-unit contribution"
    )
    steady1_candidate = next(c for c in candidates if c["session_id"] == "steady1")
    # §5.3 sizing/ranking still uses the reference-unit contribution
    # (unchanged) -- only the §5.2 floor GATE itself switched quantities.
    assert steady1_candidate["contribution"] == pytest.approx(0.3, abs=1e-9)


# ========================== (a)+(b) per-account breach =======================

def test_account_breach_meetable_floor_filters_low_share_non_rising():
    """Per-account view: contribution is `rate * account_shares[bound_acct]`
    directly (no ratio conversion, §5.3), compared straight against
    `required_reduction_pct_per_min`.

    wf2=40 (workflow, .9) score 36; sub2=30 (subagent-heavy, 1.0) score 30;
    cl2=10 is BELOW share_floor_pct=15 and NOT rising -> excluded outright.
    threshold = 40*1.5 = 60; walk: 40 -> 70 (>=60, stop) -- cl2 never needed.
    """
    wf2 = _session("wf2", "workflow", rate=40.0, trend="steady",
                    account_shares={"acctX": 1.0})
    sub2 = _session("sub2", "subagent-heavy", rate=30.0, trend="steady",
                     account_shares={"acctX": 1.0})
    cl2 = _session("cl2", "committee-loop", rate=10.0, trend="steady",
                    account_shares={"acctX": 1.0})
    int2 = _session("int2", "interactive", rate=1000.0, trend="steady",
                     account_shares={"acctX": 1.0})

    state = _account_state([wf2, sub2, cl2, int2], required=40.0, safety_factor=1.5)
    plan = cus.dry_run_target(state, CFG)

    target_ids = [t["session_id"] for t in plan["targets"]]
    assert "int2" not in target_ids
    assert "cl2" not in target_ids  # below floor and not rising

    assert plan["met"] is True
    assert plan["escalate"] is False
    assert plan["planned_shed"] >= plan["required"] * plan["safety_factor"]
    assert target_ids == ["wf2", "sub2"]


def test_account_breach_unmeetable_escalates_never_vacuous_clear():
    """(d) unmeetable: eligible elastic candidates (35 combined) fall well
    short of required*safety_factor (108) -- must escalate with a non-empty
    reason, and must NOT silently report a "met" plan just because it found
    *some* headroom."""
    wf3 = _session("wf3", "workflow", rate=20.0, trend="rising",
                    account_shares={"acctY": 1.0})
    sub3 = _session("sub3", "subagent-heavy", rate=15.0, trend="rising",
                     account_shares={"acctY": 1.0})
    int3 = _session("int3", "interactive", rate=200.0, trend="rising",
                     account_shares={"acctY": 1.0})

    state = _account_state([wf3, sub3, int3], required=90.0, safety_factor=1.2,
                            name="acctY", capacity_x=5)
    plan = cus.dry_run_target(state, CFG)

    assert plan["met"] is False
    assert plan["escalate"] is True
    assert isinstance(plan["reason"], str) and plan["reason"]
    assert plan["planned_shed"] < plan["required"] * plan["safety_factor"]
    assert "int3" not in [t["session_id"] for t in plan["targets"]]


# ================================ (e) tie-break ===============================

def test_tiebreak_deterministic_lexical_and_repeatable():
    """Three candidates with IDENTICAL contribution x elasticity_weight
    (same class/rate/share) -- the near-tie break must fall back to higher
    raw contribution (still tied here) then `session_id` LEXICAL order, and
    two independent runs over deep-copied inputs must produce a
    byte-identical (structurally equal) plan -- no LLM, no randomness, no
    dict/set-iteration dependence."""
    kwargs = dict(cls="workflow", rate=50.0, trend="steady",
                  account_shares={"acctZ": 1.0})
    s_charlie = _session("charlie", **kwargs)
    s_alpha = _session("alpha", **kwargs)
    s_bravo = _session("bravo", **kwargs)

    state = _account_state([s_charlie, s_alpha, s_bravo], required=10.0,
                           safety_factor=1.0, name="acctZ", capacity_x=10)

    plan1 = cus.dry_run_target(copy.deepcopy(state), copy.deepcopy(CFG))
    plan2 = cus.dry_run_target(copy.deepcopy(state), copy.deepcopy(CFG))

    assert plan1 == plan2
    assert [t["session_id"] for t in plan1["targets"]] == ["alpha"]


def test_size_reduction_walk_direct_tiebreak():
    """Direct interface test of `_size_reduction_walk` (not routed through
    `dry_run_target`): a tie on contribution x elasticity_weight AND on raw
    contribution resolves purely by session_id lexical order."""
    candidates = [
        {"session_id": "b", "class": "workflow", "trend": "steady",
         "contribution": 20.0, "elasticity_weight": 0.9},
        {"session_id": "a", "class": "subagent-heavy", "trend": "steady",
         "contribution": 20.0, "elasticity_weight": 0.9},
    ]
    plan = cus._size_reduction_walk(candidates, required=10.0, safety_factor=1.0,
                                    config=CFG)
    assert plan["met"] is True
    assert plan["escalate"] is False
    assert [t["session_id"] for t in plan["targets"]] == ["a"]


# ============================ extra §5.4 safety net ===========================

def test_no_breach_is_trivially_met_not_a_vacuous_clear():
    """`binding is None` (`level == "ok"`) is NOT the §5.4 vacuous-clear
    case -- there genuinely is no breach to target."""
    state = {
        "level": "ok",
        "reference_x": 5.0,
        "safety_factor": 1.2,
        "binding": None,
        "pool": _pool_block(),
        "accounts": {},
        "sessions": [],
    }
    plan = cus.dry_run_target(state, CFG)
    assert plan == {"targets": [], "planned_shed": 0.0, "required": 0.0,
                     "safety_factor": 1.2, "met": True, "escalate": False,
                     "reason": None}


def test_fable_weekly_binding_escalates_rather_than_sizing():
    """A level-bound Fable-weekly critical (design doc §3 Outputs) IS a
    real breach with no numeric required reduction to walk (§5.4: "skips
    §5.2/§5.3 sizing" for a qualitative ask this task does not build) --
    must escalate, never default `required` to 0 and vacuously clear."""
    state = {
        "level": "critical",
        "reference_x": 5.0,
        "safety_factor": 1.2,
        "binding": {"view": "account", "name": "acctQ",
                    "constraint": "fable_weekly", "window": None,
                    "eta_min": None},
        "pool": _pool_block(),
        "accounts": {"acctQ": _acct(10)},
        "sessions": [],
    }
    plan = cus.dry_run_target(state, CFG)
    assert plan["met"] is False
    assert plan["escalate"] is True
    assert isinstance(plan["reason"], str) and plan["reason"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
