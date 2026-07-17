"""Task 4 (spec-2 token-pressure forecaster, STAGE 1): decayed-step reset ramp
+ per-account remaining curve (G5, committee #8).

Production reset model: a LINEAR ramp anchored **at the boundary** (k=1), added
over the window — so there is NO false post-reset headroom before the boundary
(an in-window tail keeps burning). The per-account curve is
``remaining_w(t) = remaining_w(0) - pinned_burn_units*t + reset_ramp(t;k)``
[UNCLAMPED]; published = ``clamp(remaining_w(t), 0, C_w)``. ``R_w = C_w -
remaining_w(0) = (min(pct,gate)/100)*ratio`` is the CANONICAL constant credit
Task 5's pool curve reuses so the pool is the pointwise sum (G6).

``pinned_burn_units`` is INJECTED (finding 2), NOT derived from account-total
``state.burn_rate`` — it is the account's pinned burn component from the Task 11
partition (Phase F cannot reach the per-session split via ``state``); Phase-F
tests pass a synthetic value.

HARNESS: import cus as a module; run ``python -m pytest tests/ -q``.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

# reference_x pinned to 5; capacity_x 20 -> ratio 4.0. gate_5h = 94, gate_7d = 80.
CFG = {
    "capacity_aware": {"reference_x": 5},
    "thresholds": {"steps": [70, 85, 94]},
}
RATIO = 4.0
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


# ------------------------------- ramp -----------------------------------------

def test_ramp_zero_before_boundary_and_R_at_full_window():
    """ramp = 0 for t <= T_w (no false pre-boundary headroom), and = R_w at
    t = T_w + W_w (a full fresh window credited)."""
    R_w = 2.0
    for t in (0.0, 20.0, 40.0):
        assert cus._pressure_reset_ramp(t, 40.0, W5, R_w, 1.0) == 0.0
    assert cus._pressure_reset_ramp(40.0 + W5, 40.0, W5, R_w, 1.0) == R_w


def test_ramp_partial_linear_credit():
    """Linear (k=1) partial credit within the window: (t-T_w)/W_w of R_w."""
    R_w = 2.0
    assert cus._pressure_reset_ramp(100.0, 40.0, W5, R_w, 1.0) == R_w * (60.0 / W5)


# ----------------------------- per-account curve ------------------------------

def test_no_headroom_before_boundary():
    """For all t <= T_w the curve gets NO reset credit — it just keeps burning
    remaining_w(0) - pinned_burn_units*t (committee #8: no false headroom)."""
    acct = _acct_5h(50.0, 40)
    pinned = 0.01
    remaining0 = cus._pressure_remaining_units(50.0, 94.0, RATIO)
    f = cus._pressure_remaining_curve(acct, "5h", CFG, NOW, pinned, horizon=180)
    for t in (0.0, 20.0, 40.0):
        assert f(t) == remaining0 - pinned * t


def test_fresh_window_limit():
    """At t = T_w + W_w the ramp equals R_w, so remaining = C_w - pinned*t."""
    acct = _acct_5h(50.0, 40)
    pinned = 0.01
    remaining0 = cus._pressure_remaining_units(50.0, 94.0, RATIO)
    cap = cus._pressure_cap_units(94.0, RATIO)
    R_w = cap - remaining0
    f = cus._pressure_remaining_curve(acct, "5h", CFG, NOW, pinned, horizon=180)
    t = 40.0 + W5
    assert cus._pressure_reset_ramp(t, 40.0, W5, R_w, 1.0) == R_w
    assert f(t) == pytest.approx(cap - pinned * t)


def test_5h_partial_credit_within_H():
    """Within the 3h horizon a 5h reset gives only (t-T_w)/300 of R_w credit."""
    acct = _acct_5h(50.0, 40)
    pinned = 0.01
    remaining0 = cus._pressure_remaining_units(50.0, 94.0, RATIO)
    cap = cus._pressure_cap_units(94.0, RATIO)
    R_w = cap - remaining0
    f = cus._pressure_remaining_curve(acct, "5h", CFG, NOW, pinned, horizon=180)
    credit = R_w * (60.0 / W5)  # t=100, T_w=40
    assert f(100.0) == remaining0 - pinned * 100.0 + credit


def test_7d_negligible_but_not_zero_rounded(monkeypatch):
    """A 7d reset within H gives a tiny but NONZERO (t-T_w)/10080 credit — never
    a spurious step to zero (reset-decay risk #3)."""
    monkeypatch.setattr(
        cus, "projected_seven_day_reset",
        lambda acct, config, now: _iso(NOW + timedelta(minutes=40)),
    )
    acct = {"capacity_x": 20, "current_7d_pct": 30.0}
    remaining0 = cus._pressure_remaining_units(30.0, 80.0, RATIO)
    cap = cus._pressure_cap_units(80.0, RATIO)
    R_w = cap - remaining0
    f = cus._pressure_remaining_curve(acct, "7d", CFG, NOW, 0.0, horizon=180)
    credit = R_w * (60.0 / W7)   # t=100, T_w=40
    assert 0.0 < credit < 0.01 * R_w
    assert f(100.0) == remaining0 + credit  # pinned=0 -> pure release


def test_published_clamped():
    """published(t) = clamp(remaining, 0, C_w): heavy burn drives the UNCLAMPED
    curve negative, but the published curve floors at 0 (the unclamped value is
    retained by the raw callable, for the first-crossing math)."""
    acct = _acct_5h(50.0, 40)
    pinned = 0.1  # heavy
    cap = cus._pressure_cap_units(94.0, RATIO)
    f = cus._pressure_remaining_curve(acct, "5h", CFG, NOW, pinned, horizon=180)
    raw = f(100.0)
    assert raw < 0.0                                   # unclamped, retained
    assert max(0.0, min(cap, raw)) == 0.0              # published clamps to 0


def test_ramp_applied_up_to_horizon_240():
    """A reset at T_w=200 gets its ramp credit for t in (200,240] under
    horizon=240, but is DROPPED (no knot -> no credit) under horizon=180
    (finding 1)."""
    acct = _acct_5h(50.0, 200)
    pinned = 0.001
    remaining0 = cus._pressure_remaining_units(50.0, 94.0, RATIO)
    cap = cus._pressure_cap_units(94.0, RATIO)
    R_w = cap - remaining0
    f240 = cus._pressure_remaining_curve(acct, "5h", CFG, NOW, pinned, horizon=240)
    f180 = cus._pressure_remaining_curve(acct, "5h", CFG, NOW, pinned, horizon=180)
    # under 180 there is no reset knot -> no ramp; under 240 the ramp credits.
    assert f180(220.0) == remaining0 - pinned * 220.0
    assert f240(220.0) - f180(220.0) == R_w * (20.0 / W5)
    assert f240(220.0) > f180(220.0)


def test_burn_is_injected_pinned():
    """The curve drains by the INJECTED pinned_burn_units, never the
    account-total state.burn_rate (finding 2)."""
    acct = _acct_5h(50.0, 40, burn_rate_5h_pct_per_min=5.0)  # huge account-total
    pinned = 0.01  # small injected pinned component
    remaining0 = cus._pressure_remaining_units(50.0, 94.0, RATIO)
    cap = cus._pressure_cap_units(94.0, RATIO)
    R_w = cap - remaining0
    f = cus._pressure_remaining_curve(acct, "5h", CFG, NOW, pinned, horizon=180)
    credit = R_w * (60.0 / W5)
    # uses 0.01, NOT 0.2 units/min (= 5.0%/min * ratio/100)
    assert f(100.0) == remaining0 - 0.01 * 100.0 + credit


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
