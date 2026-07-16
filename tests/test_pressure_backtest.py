"""Task 30 (spec-2 token-pressure forecaster, STAGE 1): shadow-week BACKTEST
scorer -- `score_shadow_window(records)` pairs each forecast record with the
realized outcome N cycles later and scores forecast-vs-actual, over/under-
throttle, and reset over/under-prediction. `shadow_report` (Task 27)
evaluates the flip GATES; this is the fuller backtest feeding its verdict
inputs and the risk-4 tunability review.

HARNESS (same as every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file; add the repo root to ``sys.path`` and
``import cus``. `score_shadow_window(records)` is a pure function over an
already-parsed record list (no shadow_dir/config/now) -- no monkeypatching
needed.

Run: ``python3 -m pytest tests/test_pressure_backtest.py -q``.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# fixture builders (mirrors tests/test_pressure_shadow_report.py's own
# `_acct`/`_rec` helpers, extended for Task 30's richer `binding`/`would_ask`
# shapes)
# --------------------------------------------------------------------------

def _acct(capacity_x=20, r5h=None, r7d=None, req5h=None, req7d=None):
    """One `per_account[<name>]` entry: `capacity_x` plus whichever
    windows' `remaining_units`/`required_reduction_pct_per_min` a given test
    needs."""
    out = {"capacity_x": capacity_x}
    if r5h is not None:
        out.setdefault("5h", {})["remaining_units"] = r5h
    if r7d is not None:
        out.setdefault("7d", {})["remaining_units"] = r7d
    if req5h is not None:
        out.setdefault("5h", {})["required_reduction_pct_per_min"] = req5h
    if req7d is not None:
        out.setdefault("7d", {})["required_reduction_pct_per_min"] = req7d
    return out


def _binding(name="A", window="5h", eta_min=30.0):
    """A real production-shaped `binding` (`_pressure_binding_view`, Task
    20/9): `{view, name, constraint, window, eta_min}`."""
    return {"view": "account", "name": name, "constraint": window,
            "window": window, "eta_min": eta_min}


def _would_ask(required=0.0, planned_shed=0.0, safety_factor=1.0,
               met=True, escalate=False):
    return {"required": required, "planned_shed": planned_shed,
            "safety_factor": safety_factor, "met": met,
            "escalate": escalate, "reason": None}


def _rec(ts, *, per_account=None, pool=None, binding=None, level=None,
         would_ask=None, would_target=None, decayed=None, rolling=None,
         actual=None):
    """One shadow-log line. Only the blocks a given test actually needs are
    attached, matching `tests/test_pressure_shadow_report.py`'s own `_rec`
    convention."""
    rec = {"ts": ts, "binding": binding, "level": level}
    if per_account is not None:
        rec["per_account"] = per_account
    if pool is not None:
        rec["pool"] = pool
    if would_ask is not None:
        rec["would_ask"] = would_ask
    if would_target is not None:
        rec["would_target"] = would_target
    if decayed is not None or rolling is not None:
        rec["reset_models"] = {"decayed_step": decayed, "rolling_integral": rolling}
    if actual is not None:
        rec["reset_models_actual"] = actual
    return rec


# --------------------------------------------------------------------------
# THE most important test: under-throttle must surface (§1 safety)
# --------------------------------------------------------------------------

def test_under_throttle_surfaces():
    """Account A/5h is forecast-bound at t=0 (eta_min=30) with a plan that
    only sheds 2.0 against a (then-estimated) required of 10.0. At t=+30min
    the breach MATERIALIZES (remaining_units goes negative) and the
    record's own `required_reduction_pct_per_min` (the REALIZED severity,
    12.0) is even higher than the plan's own planned_shed -- the plan would
    have under-covered the real breach. `under_throttle_events` must
    surface exactly this, with enough detail to investigate."""
    rec0 = _rec(
        NOW.isoformat(),
        per_account={"A": _acct(r5h=5.0)},
        binding=_binding(), level="elevated",
        would_ask=_would_ask(required=10.0, planned_shed=2.0, met=False),
        would_target=["s1"],
    )
    rec1 = _rec(
        (NOW + timedelta(minutes=30)).isoformat(),
        per_account={"A": _acct(r5h=-3.0, req5h=12.0)},
    )
    metrics = cus.score_shadow_window([rec0, rec1])

    hits = metrics["under_throttle_events"]
    assert len(hits) == 1
    hit = hits[0]
    assert hit["view"] == "account"
    assert hit["name"] == "A"
    assert hit["window"] == "5h"
    assert hit["forecast_ts"] == rec0["ts"]
    assert hit["materialized_ts"] == rec1["ts"]
    assert hit["planned_shed"] == pytest.approx(2.0)
    assert hit["required_at_forecast"] == pytest.approx(10.0)
    assert hit["required_realized"] == pytest.approx(12.0)
    assert hit["deficit"] == pytest.approx(10.0)

    # Also lands in the raw materialization signal both throttle metrics derive from.
    mats = metrics["materialized_breaches"]
    assert len(mats) == 1
    assert mats[0]["name"] == "A" and mats[0]["window"] == "5h"

    # A plan that DID cover the realized severity must NOT be flagged.
    rec0_ok = _rec(
        NOW.isoformat(),
        per_account={"A": _acct(r5h=5.0)},
        binding=_binding(), level="elevated",
        would_ask=_would_ask(required=10.0, planned_shed=15.0, met=True),
        would_target=["s1"],
    )
    metrics_ok = cus.score_shadow_window([rec0_ok, rec1])
    assert metrics_ok["under_throttle_events"] == []


def test_under_throttle_immediate_breach_uses_own_record():
    """A breach that's ALREADY materialized at the forecast record itself
    (remaining_units <= 0 at t=0, eta_min=0) is its own materialization --
    no later record is required to confirm it."""
    rec0 = _rec(
        NOW.isoformat(),
        per_account={"A": _acct(r5h=-1.0, req5h=20.0)},
        binding=_binding(eta_min=0.0), level="critical",
        would_ask=_would_ask(required=20.0, planned_shed=1.0, met=False),
        would_target=["s1"],
    )
    metrics = cus.score_shadow_window([rec0])
    hits = metrics["under_throttle_events"]
    assert len(hits) == 1
    assert hits[0]["materialized_ts"] == rec0["ts"]
    assert hits[0]["required_realized"] == pytest.approx(20.0)


def test_fable_weekly_binding_produces_no_throttle_events():
    """A `fable_weekly` level-bound critical binding
    (`binding["constraint"] == "fable_weekly"`, but `binding["window"] ==
    "7d"` -- a REAL key; `_pressure_binding_view` sets `window` even for a
    fable binding) must NOT resolve to a throttle target via
    `_pressure_backtest_target`. `dry_run_target`'s own fable branch
    (cus.py ~22871) never attempts §5.2/§5.3 sizing for a qualitative
    Fable->Sonnet downshift ask -- it always returns
    `planned_shed=0.0, required=0.0, escalate=True` -- so there is no
    numeric plan to join against the account's REAL 7d `remaining_units`
    series. Even though the account's real 7d window later breaches (for an
    unrelated, ordinary reason), that must NOT surface as a spurious
    `under_throttle_events`/`materialized_breaches` hit misattributed to
    the fable record."""
    rec0 = _rec(
        NOW.isoformat(),
        per_account={"A": _acct(r7d=5.0)},
        binding={"view": "account", "name": "A", "constraint": "fable_weekly",
                 "window": "7d", "eta_min": 30.0},
        level="critical",
        would_ask=_would_ask(required=0.0, planned_shed=0.0, met=False, escalate=True),
        would_target=[],
    )
    rec1 = _rec(
        (NOW + timedelta(minutes=30)).isoformat(),
        per_account={"A": _acct(r7d=-3.0, req7d=12.0)},
    )
    metrics = cus.score_shadow_window([rec0, rec1])

    assert metrics["under_throttle_events"] == []
    assert metrics["materialized_breaches"] == []
    assert metrics["over_throttle_events"] == []


# --------------------------------------------------------------------------
# over-throttle: advisory fairness cost
# --------------------------------------------------------------------------

def test_over_throttle_on_never_breaching():
    """A plan sheds sessions at t=0 on an account/window that stays clear
    (`remaining_units` stays positive) for the ENTIRE horizon+margin window
    (240+60=300min) -- an unnecessary throttle, advisory-flagged."""
    records = [
        _rec(
            NOW.isoformat(),
            per_account={"A": _acct(r5h=5.0)},
            binding=_binding(), level="elevated",
            would_ask=_would_ask(required=5.0, planned_shed=6.0, met=True),
            would_target=["s1"],
        )
    ]
    # Cover the full 300-min deadline with clear readings every 30min.
    for m in range(30, 331, 30):
        records.append(_rec(
            (NOW + timedelta(minutes=m)).isoformat(),
            per_account={"A": _acct(r5h=5.0)},
        ))

    metrics = cus.score_shadow_window(records)
    hits = metrics["over_throttle_events"]
    assert len(hits) == 1
    assert hits[0]["view"] == "account"
    assert hits[0]["name"] == "A"
    assert hits[0]["window"] == "5h"
    assert hits[0]["targets"] == ["s1"]
    assert hits[0]["planned_shed"] == pytest.approx(6.0)

    # A never-breaching forecast should never appear as materialized or under-throttled.
    assert metrics["materialized_breaches"] == []
    assert metrics["under_throttle_events"] == []


def test_over_throttle_skipped_when_window_still_open():
    """The SAME never-breaching shape, but the shadow log stops well short
    of the horizon+margin deadline -- "haven't seen a breach yet" must NOT
    be scored as "never breaches" (that would falsely flag a shadow week
    that simply hasn't run long enough)."""
    records = [
        _rec(
            NOW.isoformat(),
            per_account={"A": _acct(r5h=5.0)},
            binding=_binding(), level="elevated",
            would_ask=_would_ask(required=5.0, planned_shed=6.0, met=True),
            would_target=["s1"],
        ),
        _rec((NOW + timedelta(minutes=30)).isoformat(), per_account={"A": _acct(r5h=5.0)}),
    ]
    metrics = cus.score_shadow_window(records)
    assert metrics["over_throttle_events"] == []


# --------------------------------------------------------------------------
# reset over/under: both models scored against the same ground truth
# --------------------------------------------------------------------------

def test_reset_over_under_scores_both_models():
    """`decayed_step` predicts remaining=20.0 and `rolling_integral`
    predicts remaining=15.0 for A/5h @+60min; the actual (joined from the
    later record's `reset_models_actual`) is 5.0. Both models over-
    predicted (positive sign = too optimistic); `rolling_integral` is
    scored ALONGSIDE `decayed_step` against the SAME ground truth, never
    gating anything itself."""
    rec0 = _rec(
        NOW.isoformat(),
        decayed={"A": {"5h": {"remaining_at_plus_60": 20.0}}},
        rolling={"A": {"5h": {"remaining_at_plus_60": 15.0}}},
    )
    rec1 = _rec(
        (NOW + timedelta(minutes=60)).isoformat(),
        actual={"A": {"5h": 5.0}},
    )
    metrics = cus.score_shadow_window([rec0, rec1])

    reset_over_under = metrics["reset_over_under"]
    assert set(reset_over_under.keys()) == {"decayed_step", "rolling_integral"}
    assert reset_over_under["decayed_step"] == pytest.approx(15.0)  # 20 - 5, over-predicted
    assert reset_over_under["rolling_integral"] == pytest.approx(10.0)  # 15 - 5, over-predicted
    assert reset_over_under["decayed_step"] != 0.0


def test_reset_over_under_negative_sign_for_underprediction():
    """The mirror case: a model that predicted LESS remaining than there
    actually was scores NEGATIVE (under-predicted / too pessimistic) --
    confirms the sign convention is `predicted - actual`, not `abs(...)`."""
    rec0 = _rec(NOW.isoformat(), decayed={"A": {"5h": {"remaining_at_plus_60": 2.0}}})
    rec1 = _rec((NOW + timedelta(minutes=60)).isoformat(), actual={"A": {"5h": 9.0}})
    metrics = cus.score_shadow_window([rec0, rec1])
    assert metrics["reset_over_under"]["decayed_step"] == pytest.approx(-7.0)


# --------------------------------------------------------------------------
# realized safety_factor series feeds the absorption criterion
# --------------------------------------------------------------------------

def test_safety_factor_series_absorption():
    """A materialized-breach record's realized `safety_factor` (1.4) is
    well under `SAFETY_FACTOR_ABSORB_CAP` (3.0) -- the absorption
    criterion ("never at 3.0 during a materialized breach") is satisfiable
    directly from `realized_safety_factor_series`."""
    rec0 = _rec(
        NOW.isoformat(),
        binding=_binding(), level="critical",
        would_ask=_would_ask(required=10.0, planned_shed=10.0, safety_factor=1.4, met=True),
    )
    metrics = cus.score_shadow_window([rec0])
    series = metrics["realized_safety_factor_series"]
    assert series == [1.4]
    assert all(sf < cus.SAFETY_FACTOR_ABSORB_CAP for sf in series)

    # A record that DOES saturate at the cap must be visible in the series too
    # (the series is a raw read, not pre-filtered -- the absorption check is
    # the CALLER's job, same as `shadow_report`'s own SOFT residual gate).
    rec_sat = _rec(
        NOW.isoformat(),
        binding=_binding(), level="critical",
        would_ask=_would_ask(required=10.0, planned_shed=10.0, safety_factor=3.0, met=True),
    )
    metrics_sat = cus.score_shadow_window([rec_sat])
    assert metrics_sat["realized_safety_factor_series"] == [3.0]


# --------------------------------------------------------------------------
# reuse-coverage: forecast_err_series/false_clears/vacuous_clears are
# populated via the REUSED Task-27 helpers, not reimplemented
# --------------------------------------------------------------------------

def test_reuse_coverage_forecast_err_false_clear_vacuous_clear():
    """One record set exercising all three reused Task-27 metrics at once:
    a FALSE_CLEAR (declares clear @+60min while currently breached, then
    re-breaches within horizon), a VACUOUS_CLEAR (a real breach with
    required>0 but an empty, non-escalating plan), and a non-empty
    forecast-error series (5h prediction joined to a later actual)."""
    rec1 = _rec(
        NOW.isoformat(),
        per_account={"A": _acct(20, r5h=-1.0)},
        binding="5h", level="critical",
        decayed={"A": {"5h": {"remaining_at_plus_60": 1.0}}},
        would_ask={"required": 5.0, "escalate": False}, would_target=[],
    )
    rec2 = _rec(
        (NOW + timedelta(minutes=60)).isoformat(),
        per_account={"A": _acct(20, r5h=-0.3)},
        actual={"A": {"5h": 1.2}},
    )
    metrics = cus.score_shadow_window([rec1, rec2])

    assert metrics["false_clears"], "false_clears must surface via _pressure_shadow_false_clears"
    assert metrics["vacuous_clears"], "vacuous_clears must surface via _pressure_shadow_vacuous_clears"
    assert set(metrics["forecast_err_series"].keys()) == {"5h", "7d"}
    assert metrics["forecast_err_series"]["5h"], "5h forecast error must be populated"
    # err = |1.0 - 1.2| / (20/1.0 reference_x default) = 0.2/20 = 0.01
    assert metrics["forecast_err_series"]["5h"][0] == pytest.approx(0.01)
