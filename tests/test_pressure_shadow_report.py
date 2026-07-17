"""Task 27 (spec-2 token-pressure forecaster, STAGE 1): `cus pressure
--shadow-report` -- the G7 flip-gate scorer. One command reads the shadow
jsonl week (Task 23's per-day append log, enriched by Task 26's
`reset_models`/`reset_models_actual` and Task 11's `would_ask`/
`would_target`/`weight_fit`) and emits a PASS/FAIL verdict against every
named flip-gate constant, plus the minimum-exercise gate (a quiet week must
EXTEND, never FLIP-READY) and the elapsed-days gate (a fast-but-short window
must EXTEND, never FLIP-READY, regardless of how fast the other gates were
met). This artifact -- never prose -- is what an operator's manual flip
decision is gated on.

HARNESS (same as every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file; add the repo root to ``sys.path`` and
``import cus``. `shadow_report(shadow_dir, config, now)` takes an explicit
``shadow_dir`` (no monkeypatching needed for the pure-function tests --
every fixture writes its own isolated ``tmp_path`` subdirectory); only the
CLI wiring smoke tests at the bottom monkeypatch `cus.PRESSURE_ROOT` (same
pattern `tests/test_pressure_shadow.py`/`tests/test_pressure_cli.py` use).

Run: ``python3 -m pytest tests/test_pressure_shadow_report.py -q``.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402
from click.testing import CliRunner  # noqa: E402


NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

# reference_x pinned to 5 (the live production pin, FACT #4) -- capacity_x=20
# accounts give a clean ratio of 4.0 each (used by the forecast-err denom
# trick below: two 20x accounts -> denom = 20/5 + 20/5 = 8.0).
BASE_CFG = {
    "capacity_aware": {"enabled": True, "reference_x": 5},
    "thresholds": {"steps": [70, 85, 94]},
    "per_model_weekly": {"cap_pct": 95},
    "accounts": [{"name": "A", "capacity_x": 20}],
}

_ORDERED_WEIGHTS = {
    "input": 1.0, "output": 5.0, "cache_read": 0.1,
    "cache_create_5m": 1.25, "cache_create_1h": 2.0,
}


# --------------------------------------------------------------------------
# fixture builders
# --------------------------------------------------------------------------

def _acct(capacity_x=20, r5h=None, r7d=None):
    """One `per_account[<name>]` entry: `capacity_x` (forecast-err denom)
    plus whichever windows' `remaining_units` the test needs (exercise-gate
    crossing counts / false-clear current-breach checks)."""
    out = {"capacity_x": capacity_x}
    if r5h is not None:
        out["5h"] = {"remaining_units": r5h}
    if r7d is not None:
        out["7d"] = {"remaining_units": r7d}
    return out


def _rec(ts, *, per_account=None, decayed=None, actual=None, binding=None,
         level=None, would_ask=None, would_target=None, weight_fit=None):
    """One shadow-log line. Only the blocks a given test actually needs are
    attached -- every accessor function is documented read-only/graceful
    over a missing block (`.get(...) or {}`), so omitting an irrelevant
    block is the realistic "this cycle had nothing to say here" shape, not
    a shortcut around the real code paths."""
    rec = {"ts": ts, "binding": binding, "level": level}
    if per_account is not None:
        rec["per_account"] = per_account
    if decayed is not None:
        rec["reset_models"] = {"decayed_step": decayed, "rolling_integral": None}
    if actual is not None:
        rec["reset_models_actual"] = actual
    if would_ask is not None:
        rec["would_ask"] = would_ask
    if would_target is not None:
        rec["would_target"] = would_target
    if weight_fit is not None:
        rec["weight_fit"] = weight_fit
    return rec


def _write_shadow(shadow_dir, filename, records):
    shadow_dir.mkdir(parents=True, exist_ok=True)
    path = shadow_dir / filename
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")
    return path


def _clock(shadow_dir, started_dt):
    shadow_dir.mkdir(parents=True, exist_ok=True)
    (shadow_dir / ".clock-started").write_text(started_dt.isoformat())


def _spotcheck(shadow_dir, agree=3, total=3):
    shadow_dir.mkdir(parents=True, exist_ok=True)
    (shadow_dir / "spotcheck.json").write_text(json.dumps({"agree": agree, "total": total}))


def _full_week_records(residual_fraction, safety_factors=None):
    """11 cycles (10-min spacing) alternating clear/breach for account "A":
    5 isolated breach episodes (i=1,3,5,7,9, each surrounded by clear
    cycles) -> `episodes == 5` (AT `MIN_EPISODES`); both windows flip sign
    on every one of the 10 consecutive pairs -> `reset_crossings == 20`
    (comfortably over `MIN_RESET_CROSSINGS`). The i==1 breach record also
    carries the week's one `weight_fit` block (`n_windows=200` AT
    `MIN_CLEAN_WINDOWS`, a low `condition_number`, ordering-holding
    `weights`, and the caller's `residual_fraction`); when `safety_factors`
    is given, every breach record additionally carries a `would_ask` with a
    realized `safety_factor` (the SOFT residual gate's absorption
    evidence) and a non-empty `would_target` (so genuine reductions are
    never mistaken for a VACUOUS_CLEAR)."""
    records = []
    sf_list = list(safety_factors or [])
    sf_i = 0
    for i in range(11):
        ts = (NOW + timedelta(minutes=10 * i)).isoformat()
        breached = (i % 2 == 1)
        per_account = {"A": _acct(20, r5h=(-1.0 if breached else 1.0),
                                   r7d=(-1.0 if breached else 1.0))}
        kwargs = dict(per_account=per_account,
                      binding=("5h" if breached else None),
                      level=("critical" if breached else "ok"))
        if breached and sf_list:
            sf = sf_list[sf_i % len(sf_list)]
            sf_i += 1
            kwargs["would_ask"] = {"required": 5.0, "safety_factor": sf, "escalate": False}
            kwargs["would_target"] = ["acct-x"]
        if i == 1:
            kwargs["weight_fit"] = {
                "weights": dict(_ORDERED_WEIGHTS),
                "residual_fraction": residual_fraction,
                "condition_number": 10.0,
                "n_windows": 200,
            }
        records.append(_rec(ts, **kwargs))
    return records


def _quiet_week_records():
    """7 cycles, only 3 isolated breach episodes (i=1,3,5) -> episodes=3,
    below MIN_EPISODES=5 -- despite 12 reset_crossings (over MIN_RESET_
    CROSSINGS=10) and a PERFECT forecast (i=0's 60-min-ahead prediction for
    account A/5h is joined at i=6, exactly on target, zero error)."""
    records = []
    for i in range(7):
        ts = (NOW + timedelta(minutes=10 * i)).isoformat()
        breached = (i % 2 == 1)
        kwargs = dict(
            per_account={"A": _acct(20, r5h=(-1.0 if breached else 1.0),
                                     r7d=(-1.0 if breached else 1.0))},
            binding=("5h" if breached else None),
            level=("critical" if breached else "ok"),
        )
        if i == 0:
            kwargs["decayed"] = {"A": {"5h": {"remaining_at_plus_60": 2.0}}}
        if i == 6:
            kwargs["actual"] = {"A": {"5h": 2.0}}
        records.append(_rec(ts, **kwargs))
    return records


# --------------------------------------------------------------------------
# per-metric boundary tests: AT threshold PASSES, just over FAILS
# --------------------------------------------------------------------------

def test_forecast_err_5h_at_threshold_passes_just_over_fails(tmp_path):
    """Two 20x accounts -> denom = 8.0. Account A predicts/actuals exactly
    (err=0.0); account B's error is tuned to land the MEDIAN/P90 of the
    2-sample list exactly on FORECAST_ERR_MEDIAN_MAX/_P90_MAX (0.10/0.20),
    then just over (0.21 -> both median and p90 shift past threshold)."""
    def _build(actual_b):
        rec1 = _rec(
            NOW.isoformat(),
            per_account={"A": _acct(20), "B": _acct(20)},
            decayed={"A": {"5h": {"remaining_at_plus_60": 2.0}},
                     "B": {"5h": {"remaining_at_plus_60": 2.0}}},
        )
        rec2 = _rec(
            (NOW + timedelta(minutes=60)).isoformat(),
            actual={"A": {"5h": 2.0}, "B": {"5h": actual_b}},
        )
        return [rec1, rec2]

    shadow_pass = tmp_path / "pass"
    _write_shadow(shadow_pass, "d.jsonl", _build(0.4))  # err_b = 1.6/8.0 = 0.20
    report = cus.shadow_report(shadow_pass, BASE_CFG, NOW)
    m = report["per_metric"]
    assert m["forecast_err_median_5h"]["pass"] is True
    assert m["forecast_err_median_5h"]["value"] == pytest.approx(0.10)
    assert m["forecast_err_p90_5h"]["pass"] is True
    assert m["forecast_err_p90_5h"]["value"] == pytest.approx(0.20)

    shadow_fail = tmp_path / "fail"
    _write_shadow(shadow_fail, "d.jsonl", _build(0.32))  # err_b = 1.68/8.0 = 0.21
    report2 = cus.shadow_report(shadow_fail, BASE_CFG, NOW)
    m2 = report2["per_metric"]
    assert m2["forecast_err_median_5h"]["pass"] is False
    assert m2["forecast_err_p90_5h"]["pass"] is False


def test_weight_cv_at_threshold_passes_just_over_fails(tmp_path):
    """Only "output" clears the >=10%-mass-share bar (the other 4 columns
    stay near ~9% of a record's total weighted mass and are excluded);
    output=[7.5,12.5] -> CV exactly 0.25 (pstdev=2.5, mean=10.0); output=
    [7.4,12.6] -> CV ~0.26 (pstdev~2.6), just over."""
    def _wf(output):
        return {"weights": {"output": output, "input": 1.0, "cache_read": 0.1,
                             "cache_create_5m": 1.0, "cache_create_1h": 1.0}}

    shadow_pass = tmp_path / "pass"
    recs = [_rec((NOW + timedelta(minutes=10 * i)).isoformat(), weight_fit=_wf(v))
            for i, v in enumerate([7.5, 12.5])]
    _write_shadow(shadow_pass, "d.jsonl", recs)
    report = cus.shadow_report(shadow_pass, BASE_CFG, NOW)
    assert report["per_metric"]["weight_cv"]["pass"] is True
    assert report["per_metric"]["weight_cv"]["value"] == pytest.approx(0.25)

    shadow_fail = tmp_path / "fail"
    recs2 = [_rec((NOW + timedelta(minutes=10 * i)).isoformat(), weight_fit=_wf(v))
             for i, v in enumerate([7.4, 12.6])]
    _write_shadow(shadow_fail, "d.jsonl", recs2)
    report2 = cus.shadow_report(shadow_fail, BASE_CFG, NOW)
    assert report2["per_metric"]["weight_cv"]["pass"] is False


def test_min_clean_windows_at_threshold_and_just_under(tmp_path):
    shadow_pass = tmp_path / "pass"
    _write_shadow(shadow_pass, "d.jsonl",
                  [_rec(NOW.isoformat(), weight_fit={"n_windows": 200})])
    report = cus.shadow_report(shadow_pass, BASE_CFG, NOW)
    assert report["per_metric"]["min_clean_windows"]["pass"] is True
    assert report["per_metric"]["min_clean_windows"]["value"] == 200.0

    shadow_fail = tmp_path / "fail"
    _write_shadow(shadow_fail, "d.jsonl",
                  [_rec(NOW.isoformat(), weight_fit={"n_windows": 199})])
    report2 = cus.shadow_report(shadow_fail, BASE_CFG, NOW)
    assert report2["per_metric"]["min_clean_windows"]["pass"] is False


def test_fit_r2_median_near_threshold(tmp_path):
    """r2 = 1 - residual_fraction**2 is DERIVED (fit_burn_weights never
    publishes r2 directly), so an exact-threshold construction is float-
    fragile via a sqrt/square round-trip; this instead brackets the
    threshold with a small margin on each side (0.44 -> r2~0.8064 pass,
    0.46 -> r2~0.7884 fail) and asserts the inequality DIRECTION against
    the live `cus.FIT_R2_MEDIAN_MIN` constant, not a hardcoded literal."""
    shadow_pass = tmp_path / "pass"
    _write_shadow(shadow_pass, "d.jsonl",
                  [_rec(NOW.isoformat(), weight_fit={"residual_fraction": 0.44})])
    report = cus.shadow_report(shadow_pass, BASE_CFG, NOW)
    m = report["per_metric"]["fit_r2_median"]
    assert m["pass"] is True
    assert m["value"] == pytest.approx(1.0 - 0.44 ** 2)
    assert m["value"] >= cus.FIT_R2_MEDIAN_MIN

    shadow_fail = tmp_path / "fail"
    _write_shadow(shadow_fail, "d.jsonl",
                  [_rec(NOW.isoformat(), weight_fit={"residual_fraction": 0.46})])
    report2 = cus.shadow_report(shadow_fail, BASE_CFG, NOW)
    m2 = report2["per_metric"]["fit_r2_median"]
    assert m2["pass"] is False
    assert m2["value"] < cus.FIT_R2_MEDIAN_MIN


def test_cond_median_at_threshold_and_just_over(tmp_path):
    shadow_pass = tmp_path / "pass"
    recs = [_rec((NOW + timedelta(minutes=10 * i)).isoformat(), weight_fit={"condition_number": c})
            for i, c in enumerate([0.0, 30.0, 40.0])]
    _write_shadow(shadow_pass, "d.jsonl", recs)
    report = cus.shadow_report(shadow_pass, BASE_CFG, NOW)
    assert report["per_metric"]["cond_median"]["pass"] is True
    assert report["per_metric"]["cond_median"]["value"] == 30.0

    shadow_fail = tmp_path / "fail"
    recs2 = [_rec((NOW + timedelta(minutes=10 * i)).isoformat(), weight_fit={"condition_number": c})
             for i, c in enumerate([0.0, 31.0, 40.0])]
    _write_shadow(shadow_fail, "d.jsonl", recs2)
    report2 = cus.shadow_report(shadow_fail, BASE_CFG, NOW)
    assert report2["per_metric"]["cond_median"]["pass"] is False


def test_cond_p90_at_threshold_and_just_over(tmp_path):
    shadow_pass = tmp_path / "pass"
    recs = [_rec((NOW + timedelta(minutes=10 * i)).isoformat(), weight_fit={"condition_number": c})
            for i, c in enumerate([1.0, 5.0, 100.0])]
    _write_shadow(shadow_pass, "d.jsonl", recs)
    report = cus.shadow_report(shadow_pass, BASE_CFG, NOW)
    assert report["per_metric"]["cond_p90"]["pass"] is True
    assert report["per_metric"]["cond_p90"]["value"] == 100.0

    shadow_fail = tmp_path / "fail"
    recs2 = [_rec((NOW + timedelta(minutes=10 * i)).isoformat(), weight_fit={"condition_number": c})
             for i, c in enumerate([1.0, 5.0, 101.0])]
    _write_shadow(shadow_fail, "d.jsonl", recs2)
    report2 = cus.shadow_report(shadow_fail, BASE_CFG, NOW)
    assert report2["per_metric"]["cond_p90"]["pass"] is False


def test_ordering_priors_held_violation_fails_all_held_passes(tmp_path):
    good = {"output": 5.0, "input": 1.0, "cache_read": 0.1,
            "cache_create_5m": 1.25, "cache_create_1h": 2.0}
    bad = {"output": 5.0, "input": 1.0, "cache_read": 0.1,
           "cache_create_5m": 2.0, "cache_create_1h": 1.25}  # 5m > 1h -- violates the chain

    shadow_pass = tmp_path / "pass"
    _write_shadow(shadow_pass, "d.jsonl",
                  [_rec((NOW + timedelta(minutes=10 * i)).isoformat(), weight_fit={"weights": good})
                   for i in range(3)])
    report = cus.shadow_report(shadow_pass, BASE_CFG, NOW)
    assert report["per_metric"]["ordering_priors_held"]["pass"] is True
    assert report["per_metric"]["ordering_priors_held"]["value"] == 1.0

    shadow_mixed = tmp_path / "mixed"
    _write_shadow(shadow_mixed, "d.jsonl", [
        _rec(NOW.isoformat(), weight_fit={"weights": good}),
        _rec((NOW + timedelta(minutes=10)).isoformat(), weight_fit={"weights": bad}),
        _rec((NOW + timedelta(minutes=20)).isoformat(), weight_fit={"weights": good}),
    ])
    report2 = cus.shadow_report(shadow_mixed, BASE_CFG, NOW)
    assert report2["per_metric"]["ordering_priors_held"]["pass"] is False
    assert report2["per_metric"]["ordering_priors_held"]["value"] == pytest.approx(2 / 3)


# --------------------------------------------------------------------------
# SOFT residual gate: within bounds / exceeded-not-absorbed / exceeded-absorbed
# --------------------------------------------------------------------------

def test_residual_within_bounds_passes(tmp_path):
    shadow_dir = tmp_path / "shadow"
    _write_shadow(shadow_dir, "d.jsonl",
                  [_rec(NOW.isoformat(), weight_fit={"residual_fraction": 0.05})])
    report = cus.shadow_report(shadow_dir, BASE_CFG, NOW)
    m = report["per_metric"]["residual_median"]
    assert m["pass"] is True
    assert m["absorbed"] is False  # never needed -- residual was already within bounds


def test_residual_exceeded_not_absorbed_fails(tmp_path):
    """residual_fraction=0.20 exceeds RESIDUAL_MEDIAN_MAX=0.15; the week's
    one materialized breach realized a SATURATED safety_factor (3.0 ==
    SAFETY_FACTOR_ABSORB_CAP) -- absorption explicitly does not apply."""
    shadow_dir = tmp_path / "shadow"
    recs = [
        _rec(NOW.isoformat(), weight_fit={"residual_fraction": 0.20}),
        _rec((NOW + timedelta(minutes=10)).isoformat(), binding="5h", level="critical",
             would_ask={"required": 0.0, "safety_factor": 3.0, "escalate": False}),
    ]
    _write_shadow(shadow_dir, "d.jsonl", recs)
    report = cus.shadow_report(shadow_dir, BASE_CFG, NOW)
    m = report["per_metric"]["residual_median"]
    assert m["pass"] is False
    assert m["absorbed"] is False
    assert report["per_metric"]["residual_p90"]["pass"] is False


def test_residual_exceeded_empty_safety_factors_not_absorbed(tmp_path):
    """residual_fraction=0.25 exceeds RESIDUAL_MEDIAN_MAX (0.15) but still
    passes fit_r2_median (r2 = 1-0.25**2 = 0.9375 >= 0.80); the week's
    breach records carry NO `would_ask.safety_factor` at all (Task 27
    finding: absence of evidence must not read as evidence of absorption).
    Regression for the vacuous-True bug: `_pressure_median([]) == 0.0`
    used to satisfy `0.0 <= SAFETY_FACTOR_ABSORB_MEDIAN_MAX` and
    `not any([])` unconditionally, certifying "absorbed" with zero
    logged safety factors."""
    shadow_dir = tmp_path / "shadow"
    records = _full_week_records(residual_fraction=0.25)  # no safety_factors -> realized list is empty
    _write_shadow(shadow_dir, "d.jsonl", records)
    _clock(shadow_dir, NOW - timedelta(days=8))
    _spotcheck(shadow_dir)

    report = cus.shadow_report(shadow_dir, BASE_CFG, NOW)
    m = report["per_metric"]["residual_median"]
    assert m["value"] == pytest.approx(0.25)
    assert m["pass"] is False
    assert m["absorbed"] is False
    assert report["verdict"] == "BLOCKED"
    assert "residual_median" in report["blocking_metrics"]


# --------------------------------------------------------------------------
# HARD-ZERO safety gates: single injected false-clear / vacuous-clear
# --------------------------------------------------------------------------

def test_false_clear_blocks(tmp_path):
    """cycle N declares account A/5h clear 60-min out while it is CURRENTLY
    breached; 90 minutes later (within the default 180-min horizon) the
    ACTUAL series is breached again -> one FALSE_CLEAR detection ->
    BLOCKED, naming `false_clear_count`, regardless of every other gate."""
    shadow_dir = tmp_path / "shadow"
    rec1 = _rec(NOW.isoformat(),
                per_account={"A": _acct(20, r5h=-1.0)},
                decayed={"A": {"5h": {"remaining_at_plus_60": 1.0}}},
                binding="5h", level="critical")
    rec2 = _rec((NOW + timedelta(minutes=90)).isoformat(),
                per_account={"A": _acct(20, r5h=-0.5)},
                binding="5h", level="critical")
    _write_shadow(shadow_dir, "d.jsonl", [rec1, rec2])

    report = cus.shadow_report(shadow_dir, BASE_CFG, NOW)
    assert report["verdict"] == "BLOCKED"
    assert "false_clear_count" in report["blocking_metrics"]
    assert report["per_metric"]["false_clear_count"]["value"] == 1
    assert report["per_metric"]["false_clear_count"]["pass"] is False


def test_vacuous_clear_blocks(tmp_path):
    """A real breach whose dry-run plan needed a genuine reduction
    (`required > 0`) but emitted an EMPTY target set without escalating ->
    one VACUOUS_CLEAR detection -> BLOCKED, naming `vacuous_clear_count`."""
    shadow_dir = tmp_path / "shadow"
    rec = _rec(NOW.isoformat(), binding="5h", level="critical",
               would_ask={"required": 5.0, "escalate": False}, would_target=[])
    _write_shadow(shadow_dir, "d.jsonl", [rec])

    report = cus.shadow_report(shadow_dir, BASE_CFG, NOW)
    assert report["verdict"] == "BLOCKED"
    assert "vacuous_clear_count" in report["blocking_metrics"]
    assert report["per_metric"]["vacuous_clear_count"]["value"] == 1


# --------------------------------------------------------------------------
# minimum-exercise gate + elapsed-days gate -> EXTEND
# --------------------------------------------------------------------------

def test_quiet_week_extends_despite_perfect_accuracy(tmp_path):
    shadow_dir = tmp_path / "shadow"
    _write_shadow(shadow_dir, "d.jsonl", _quiet_week_records())

    report = cus.shadow_report(shadow_dir, BASE_CFG, NOW)
    assert report["exercise_gate"]["episodes"] == 3
    assert report["exercise_gate"]["reset_crossings"] >= cus.MIN_RESET_CROSSINGS
    assert report["exercise_gate"]["met"] is False
    assert report["per_metric"]["forecast_err_median_5h"]["value"] == 0.0  # perfect accuracy
    assert report["verdict"] == "EXTEND"


def test_under_7_days_extends(tmp_path):
    """Exercise gate AND every hard gate met, but the clock started only 4
    days ago -> EXTEND with days_remaining=3, NOT FLIP-READY (finding 9)."""
    shadow_dir = tmp_path / "shadow"
    _write_shadow(shadow_dir, "d.jsonl", _full_week_records(residual_fraction=0.05))
    _clock(shadow_dir, NOW - timedelta(days=4))
    _spotcheck(shadow_dir)

    report = cus.shadow_report(shadow_dir, BASE_CFG, NOW)
    assert report["verdict"] == "EXTEND"
    assert report["exercise_gate"]["met"] is True
    assert report["elapsed_days"] == pytest.approx(4.0)
    assert report["days_remaining"] == pytest.approx(3.0)
    assert report["blocking_metrics"] == []


def test_absent_clock_marker_extends(tmp_path):
    """No `.clock-started` at all (Task 31 has not written it yet) ->
    EXTEND, elapsed_days=0, days_remaining=7, NEVER an exception, NEVER
    FLIP-READY (finding 4) -- even though every other gate is met."""
    shadow_dir = tmp_path / "shadow"
    _write_shadow(shadow_dir, "d.jsonl", _full_week_records(residual_fraction=0.05))
    _spotcheck(shadow_dir)
    # deliberately no _clock() call.

    report = cus.shadow_report(shadow_dir, BASE_CFG, NOW)
    assert report["verdict"] == "EXTEND"
    assert report["elapsed_days"] == 0.0
    assert report["days_remaining"] == 7.0


def test_absent_shadow_dir_extends_gracefully(tmp_path):
    """A shadow directory that has never been created at all (Task 31's
    deploy smoke calling --shadow-report before the first cycle) must never
    raise -- graceful EXTEND with an all-empty exercise gate."""
    missing = tmp_path / "does-not-exist"
    report = cus.shadow_report(missing, BASE_CFG, NOW)
    assert report["verdict"] == "EXTEND"
    assert report["elapsed_days"] == 0.0
    assert report["days_remaining"] == 7.0
    assert report["exercise_gate"]["episodes"] == 0
    assert report["blocking_metrics"] == []


# --------------------------------------------------------------------------
# whole-verdict FLIP-READY: full happy path + soft-residual-absorbed
# --------------------------------------------------------------------------

def test_flip_ready_full_happy_path(tmp_path):
    shadow_dir = tmp_path / "shadow"
    _write_shadow(shadow_dir, "d.jsonl", _full_week_records(residual_fraction=0.05))
    _clock(shadow_dir, NOW - timedelta(days=8))
    _spotcheck(shadow_dir)

    report = cus.shadow_report(shadow_dir, BASE_CFG, NOW)
    assert report["verdict"] == "FLIP-READY"
    assert report["blocking_metrics"] == []
    assert report["exercise_gate"]["met"] is True


def test_soft_residual_exceeded_but_absorbed_flip_ready(tmp_path):
    """residual_fraction=0.20 exceeds RESIDUAL_MEDIAN_MAX (0.15), but every
    materialized breach that week realized a conservative safety_factor
    (median 1.2 <= 1.5, never at the 3.0 cap) -- the SOFT gate is absorbed,
    and (with every other gate + the exercise/elapsed-days gates satisfied)
    the overall verdict is still FLIP-READY."""
    shadow_dir = tmp_path / "shadow"
    records = _full_week_records(residual_fraction=0.20,
                                  safety_factors=[1.2, 1.3, 1.2, 1.3, 1.2])
    _write_shadow(shadow_dir, "d.jsonl", records)
    _clock(shadow_dir, NOW - timedelta(days=8))
    _spotcheck(shadow_dir)

    report = cus.shadow_report(shadow_dir, BASE_CFG, NOW)
    m = report["per_metric"]["residual_median"]
    assert m["value"] == pytest.approx(0.20)
    assert m["pass"] is True
    assert m["absorbed"] is True
    assert report["verdict"] == "FLIP-READY"
    assert report["blocking_metrics"] == []


# --------------------------------------------------------------------------
# CLI wiring smoke tests
# --------------------------------------------------------------------------

def _cli_env(tmp_path, monkeypatch):
    """Same pattern as `tests/test_pressure_cli.py`'s `_env`, extended with
    `PRESSURE_ROOT` (`tests/test_pressure_shadow.py`'s pattern) so
    `_pressure_shadow_dir()` resolves inside the isolated tmp tree."""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"accounts": {"A": {"capacity_x": 20}}}) + "\n")
    monkeypatch.setattr(cus, "STATE_JSON", state_path)

    config_path = tmp_path / "config.yaml"
    cus.write_yaml(config_path, BASE_CFG)
    monkeypatch.setattr(cus, "CONFIG_YAML", config_path)

    monkeypatch.setattr(cus, "CLAUDE_DIR", tmp_path / "claude_home")
    monkeypatch.setattr(cus, "SESSIONS_LOG", tmp_path / "sessions.log")
    monkeypatch.setattr(cus, "PRESSURE_ROOT", tmp_path / "claude-accounts" / "pressure")


def test_cli_shadow_report_table_smoke(tmp_path, monkeypatch):
    _cli_env(tmp_path, monkeypatch)
    result = CliRunner().invoke(cus.cli, ["pressure", "--shadow-report"])
    assert result.exit_code == 0, result.output
    assert "verdict: EXTEND" in result.output
    assert "exercise_gate:" in result.output
    assert "elapsed_days:" in result.output


def test_cli_shadow_report_json_smoke(tmp_path, monkeypatch):
    _cli_env(tmp_path, monkeypatch)
    result = CliRunner().invoke(cus.cli, ["pressure", "--shadow-report", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["verdict"] == "EXTEND"
    assert report["elapsed_days"] == 0.0
    assert "per_metric" in report


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
