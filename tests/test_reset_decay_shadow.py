"""Task 26 (spec-2 token-pressure forecaster, STAGE 1): reset-decay dual-model
shadow scoring + ``ramp_k`` calibration log.

Ships `decayed_step, k=1` (Task 4) as the ONLY live curve, unchanged, but adds
a SECOND model -- `rolling_integral` -- computed ONLY in shadow:

    remaining_w(t) = remaining_w(0) + (R_w/W_w)*min(t, W_w) - pinned_burn_units*t

(release starts at t=0 -- the uniform-history closed form -- vs `decayed_step`'s
release anchored at the boundary `T_w`). It reuses the EXACT SAME `R_w`/`W_w`/
`remaining_w(0)` `decayed_step` computes, and is gated on the same reset-in-
horizon `T_w is not None` check, so a stale `five_hour_resets_at` that rolls
forward past the horizon (Task 3) is never credited early by either model.
LOW-CONFIDENCE (needs a burn-history profile cus doesn't store); shadow-only,
G5 -- never gates. `decayed_step` stays the ONLY model the live ETA/level/emit
path ever computes (no production caller passes `model=`).

The shadow record's `reset_models` block (Task 23's own `rolling_integral:
None` placeholder, now wired) logs BOTH models' per-account/window
`eta_min`/`remaining_at_plus_60` each cycle, plus `reset_models_actual` (the
CURRENT actual remaining, straight off the snapshot) -- Task 30's backtest
temporally-joins a cycle-N prediction to the cycle-~N+60min actual; the join
itself is not built here.

HARNESS (same as every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file; add the repo root to ``sys.path`` and
``import cus``.

Run: ``python3 -m pytest tests/test_reset_decay_shadow.py -q``.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

# ============================================================================
# Curve-level fixtures (tests 1-4) -- same shape as tests/test_pressure_curve.py
# ============================================================================

CFG = {
    "capacity_aware": {"reference_x": 5},
    "thresholds": {"steps": [70, 85, 94]},   # gate_5h = 94
}
RATIO = 4.0     # capacity_x 20 / reference_x 5
W5 = 300.0
W7 = 10080.0


def _iso(dt):
    return dt.isoformat()


def _acct_5h(pct, reset_min_from_now, **extra):
    a = {"capacity_x": 20, "current_5h_pct": pct}
    if reset_min_from_now is not None:
        a["five_hour_resets_at"] = _iso(NOW + timedelta(minutes=reset_min_from_now))
    a.update(extra)
    return a


# ============================================================================
# Snapshot-level fixtures (tests 5-6) -- same shape as tests/test_pressure_json.py
# ============================================================================

CFG_FULL = {
    "capacity_aware": {"reference_x": 5},
    "thresholds": {"steps": [70, 85, 94]},        # gate_5h = 94 (live top)
    "per_model_weekly": {"cap_pct": 95},
    "pressure": {"critical_eta_min": 60, "weekly_gate_margin_pct": 2,
                 "horizon_hours": 3, "exit_margin_hours": 1},
    "accounts": [{"name": "A", "capacity_x": 20}],
}

WEIGHT_FIT = {
    "weights": {"output": 1.0, "input": 0.1, "cache_read": 0.05,
                "cache_create_5m": 0.2, "cache_create_1h": 0.4},
    "source": "fit",
    "condition_number": 10.0,
    "residual_fraction": 0.05,
    "n_windows": 250,
}

ATTRIBUTION = {
    "confidence": 1.0,
    "blindness": False,
    "residual_fraction": 0.05,
    "reason": "steady-state: 5/5 sessions read",
}


class FakePartition:
    """Synthetic Task-11 ``_partition_burn`` double (same protocol every
    Phase-F test file uses): disjoint pinned vs rotatable burn in units/min,
    keyed by (name, window)."""

    def __init__(self, pinned=None, rotatable=None):
        self._pinned = dict(pinned or {})
        self._rotatable = dict(rotatable or {})

    def pinned_burn_units(self, name, window):
        return self._pinned.get((name, window), 0.0)

    def rotatable_burn_units(self, name, window):
        return self._rotatable.get((name, window), 0.0)


def _acct(pct=50.0, pct7d=10.0, reset_min=500, **extra):
    a = {"capacity_x": 20, "current_5h_pct": pct, "current_7d_pct": pct7d,
         "last_poll_ts": _iso(NOW)}
    if reset_min is not None:
        a["five_hour_resets_at"] = _iso(NOW + timedelta(minutes=reset_min))
    a.update(extra)
    return a


def _state(accounts):
    return {"accounts": accounts}


def _snapshot(state, partition, cfg=CFG_FULL, now=NOW):
    return cus._pressure_snapshot(
        state, cfg, now,
        partition=partition,
        session_table=[],
        weight_fit=dict(WEIGHT_FIT),
        attribution=dict(ATTRIBUTION),
    )


# ============================================================================
# 1. rolling_integral releases from t=0
# ============================================================================

def test_rolling_integral_releases_from_t0():
    """`rolling_integral` credits `(R_w/W_w)*t` starting immediately at t=0,
    while `decayed_step` gives NO credit before the boundary `T_w` -- a
    concrete point (t=20, T_w=40) where the two curves diverge."""
    acct = _acct_5h(50.0, 40)   # T_w = 40
    pinned = 0.0
    remaining0 = cus._pressure_remaining_units(50.0, 94.0, RATIO)   # 1.76
    cap = cus._pressure_cap_units(94.0, RATIO)                      # 3.76
    R_w = cap - remaining0                                          # 2.0

    f_decayed = cus._pressure_remaining_curve(
        acct, "5h", CFG, NOW, pinned, horizon=180, model="decayed_step")
    f_rolling = cus._pressure_remaining_curve(
        acct, "5h", CFG, NOW, pinned, horizon=180, model="rolling_integral")

    # t=20 < T_w=40: decayed_step has NOT started releasing yet.
    assert f_decayed(20.0) == pytest.approx(remaining0)
    # rolling_integral already released (R_w/W_w)*20 by t=20 (from t=0).
    expected_rolling = remaining0 + (R_w / W5) * 20.0
    assert f_rolling(20.0) == pytest.approx(expected_rolling)
    assert f_rolling(20.0) != pytest.approx(f_decayed(20.0))
    assert f_rolling(20.0) > f_decayed(20.0)   # more headroom already released


# ============================================================================
# 2. decayed_step is byte-identical (Task 4 unchanged)
# ============================================================================

def test_decayed_step_unchanged():
    """`ramp=0` for `t<=T_w`; at `t=T_w+W_w` ramp=`R_w` -- and the default
    (no `model=` kwarg) curve is identical to an explicit `model="decayed_
    step"` curve, proving the Task-26 extension left the live branch alone."""
    acct = _acct_5h(50.0, 40)
    pinned = 0.01
    remaining0 = cus._pressure_remaining_units(50.0, 94.0, RATIO)
    cap = cus._pressure_cap_units(94.0, RATIO)

    f_default = cus._pressure_remaining_curve(acct, "5h", CFG, NOW, pinned, horizon=180)
    f_explicit = cus._pressure_remaining_curve(
        acct, "5h", CFG, NOW, pinned, horizon=180, model="decayed_step")

    for t in (0.0, 20.0, 40.0):
        assert f_default(t) == pytest.approx(remaining0 - pinned * t)
        assert f_default(t) == f_explicit(t)

    t_full = 40.0 + W5
    assert f_default(t_full) == pytest.approx(cap - pinned * t_full)
    assert f_default(t_full) == f_explicit(t_full)


# ============================================================================
# 3. stale five_hour_resets_at rolls forward, not credited early, both models
# ============================================================================

def test_stale_five_hour_rolled_forward():
    """A STALE `five_hour_resets_at` (10 min in the PAST) rolls forward by a
    whole W5=300 period to T_w=290 (Task 3's own rollover rule) -- never
    credited early by EITHER model.

    Part A: under horizon=180, the rolled-forward T_w=290 falls OUTSIDE the
    horizon -> `_pressure_reset_knot` returns None -> NEITHER model applies
    any release at all (a buggy implementation that used the raw, still-
    negative timestamp -- or mis-rolled it -- would instead show an
    erroneous immediate/early credit here).

    Part B: under horizon=300 (T_w=290 now retained), both curves give
    EXACTLY zero credit at t=0 -- decayed_step because t=0 <= T_w=290;
    rolling_integral because its own release term is `(R_w/W_w)*min(t,W_w)`,
    which is 0 at t=0 regardless of how far away T_w is.
    """
    acct = _acct_5h(50.0, -10)   # stale: reset "10 min ago"
    pinned = 0.01
    remaining0 = cus._pressure_remaining_units(50.0, 94.0, RATIO)

    assert cus._pressure_reset_knot(acct, "5h", CFG, NOW, horizon=180) is None

    # Part A: rolled-forward reset lands beyond horizon -> no credit, ever.
    f_decayed_a = cus._pressure_remaining_curve(
        acct, "5h", CFG, NOW, pinned, horizon=180, model="decayed_step")
    f_rolling_a = cus._pressure_remaining_curve(
        acct, "5h", CFG, NOW, pinned, horizon=180, model="rolling_integral")
    for t in (0.0, 50.0, 150.0, 180.0):
        assert f_decayed_a(t) == pytest.approx(remaining0 - pinned * t)
        assert f_rolling_a(t) == pytest.approx(remaining0 - pinned * t)

    # Part B: horizon=300 retains the rolled-forward T_w=290 -- still zero
    # credit AT t=0 for both (no early credit from the stale timestamp).
    assert cus._pressure_reset_knot(acct, "5h", CFG, NOW, horizon=300) == pytest.approx(290.0)
    f_decayed_b = cus._pressure_remaining_curve(
        acct, "5h", CFG, NOW, pinned, horizon=300, model="decayed_step")
    f_rolling_b = cus._pressure_remaining_curve(
        acct, "5h", CFG, NOW, pinned, horizon=300, model="rolling_integral")
    assert f_decayed_b(0.0) == pytest.approx(remaining0)
    assert f_rolling_b(0.0) == pytest.approx(remaining0)


# ============================================================================
# 4. 7d ramp negligible within H, no spurious step, both models
# ============================================================================

def test_7d_ramp_negligible_within_H(monkeypatch):
    """A 7d reset within the (default) H gives a tiny but NONZERO credit --
    never a spurious step to zero -- for BOTH models independently (Task 4's
    own reset-decay risk #3, extended to rolling_integral)."""
    monkeypatch.setattr(
        cus, "projected_seven_day_reset",
        lambda acct, config, now: _iso(NOW + timedelta(minutes=40)),
    )
    acct = {"capacity_x": 20, "current_7d_pct": 30.0}
    remaining0 = cus._pressure_remaining_units(30.0, 80.0, RATIO)
    cap = cus._pressure_cap_units(80.0, RATIO)
    R_w = cap - remaining0

    f_decayed = cus._pressure_remaining_curve(
        acct, "7d", CFG, NOW, 0.0, horizon=180, model="decayed_step")
    f_rolling = cus._pressure_remaining_curve(
        acct, "7d", CFG, NOW, 0.0, horizon=180, model="rolling_integral")

    credit_decayed = f_decayed(100.0) - remaining0    # t=100, T_w=40
    credit_rolling = f_rolling(100.0) - remaining0     # t=100, release from t=0

    assert 0.0 < credit_decayed < 0.01 * R_w
    assert 0.0 < credit_rolling < 0.01 * R_w


# ============================================================================
# 5. both models logged each cycle, plus the current actual remaining
# ============================================================================

def test_both_models_logged_each_cycle():
    """The shadow record's `reset_models` has both `decayed_step` and
    `rolling_integral` populated per account/window with `eta_min`/
    `remaining_at_plus_60`, plus `reset_models_actual` -- the CURRENT actual
    remaining straight off the snapshot (Task 30's join key)."""
    acct = _acct(pct=50.0, pct7d=10.0, reset_min=40)
    state = _state({"A": acct})
    partition = FakePartition(pinned={("A", "5h"): 0.02, ("A", "7d"): 0.001})
    snapshot = _snapshot(state, partition)

    record = cus._pressure_build_shadow_record(state, snapshot, CFG_FULL, NOW)

    for model in ("decayed_step", "rolling_integral"):
        assert model in record["reset_models"]
        assert "A" in record["reset_models"][model]
        for window in ("5h", "7d"):
            entry = record["reset_models"][model]["A"][window]
            assert set(entry) == {"eta_min", "remaining_at_plus_60"}
            assert isinstance(entry["remaining_at_plus_60"], float)

    assert record["reset_models_actual"]["A"] == {
        "5h": snapshot["accounts"]["A"]["5h"]["remaining_units"],
        "7d": snapshot["accounts"]["A"]["7d"]["remaining_units"],
    }


# ============================================================================
# 6. live ETA always uses decayed_step, regardless of rolling_integral
# ============================================================================

def test_live_eta_uses_decayed_step_regardless():
    """The published/live per-account ETA (`_pressure_snapshot`'s own
    `pinned_eta_min`, decayed_step-only -- no production caller ever passes
    `model=`) equals the shadow record's `decayed_step` ETA exactly, even
    though `rolling_integral` genuinely diverges for the SAME account/window
    (a real ~20min gap: decayed_step's boundary-anchored release lags
    rolling_integral's from-t=0 release, so decayed_step crosses zero
    LATER than rolling_integral for this steadily-burning fixture)."""
    acct = _acct(pct=50.0, pct7d=10.0, reset_min=40)   # T_w = 40
    state = _state({"A": acct})
    partition = FakePartition(pinned={("A", "5h"): 0.02, ("A", "7d"): 0.001})
    snapshot = _snapshot(state, partition)

    live_eta_5h = snapshot["accounts"]["A"]["5h"]["pinned_eta_min"]
    assert live_eta_5h is not None

    record = cus._pressure_build_shadow_record(state, snapshot, CFG_FULL, NOW)
    decayed_eta = record["reset_models"]["decayed_step"]["A"]["5h"]["eta_min"]
    rolling_eta = record["reset_models"]["rolling_integral"]["A"]["5h"]["eta_min"]

    # Shadow's own decayed_step ETA matches the live-published ETA exactly
    # (same model, same pinned burn, same horizon).
    assert decayed_eta == pytest.approx(live_eta_5h, abs=1e-6)

    # rolling_integral genuinely diverges -- proves the shadow computation
    # is a REAL second model, not a no-op alias of decayed_step.
    assert rolling_eta is not None
    assert abs(rolling_eta - decayed_eta) > 5.0

    # The live snapshot's published ETA is decayed_step's, never rolling_
    # integral's -- unaffected by what shadow additionally computed.
    assert live_eta_5h == pytest.approx(decayed_eta, abs=1e-6)
    assert live_eta_5h != pytest.approx(rolling_eta, abs=1e-6)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
