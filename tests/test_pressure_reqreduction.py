"""Task 8 (spec-2 token-pressure STAGE 1): required reduction — exact closed-form
min-ratio + unmeetable detection (§5.4, G6).

The smallest cut that clears the breach to ``H + exit_margin = 240`` min, as an
exact per-knot min-ratio (linear-ramp default). The extremum of ``remaining(t)/t``
on a linear segment is at a knot, so the min over ``K_rr`` is exact. ``K_rr`` is
built over ``[0, 240]``, INCLUDING ``t=240`` itself and the ``Z_rr`` clamp-zero
roots (round-1 findings). An unmeetable/pinned-drain case (min-ratio ≤ 0) routes
to §5.4 escalate-before-gate.

HARNESS: ``python3 -m pytest tests/ -q``.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
RATIO = 4.0
CFG = {
    "capacity_aware": {"reference_x": 5},
    "thresholds": {"steps": [70, 85, 94]},  # gate_5h = 94
}


def _iso(dt):
    return dt.isoformat()


class FakePartition:
    def __init__(self, pinned=None, rotatable=None):
        self._pinned = dict(pinned or {})
        self._rotatable = dict(rotatable or {})

    def pinned_burn_units(self, name, window):
        return self._pinned.get((name, window), 0.0)

    def rotatable_burn_units(self, name, window):
        return self._rotatable.get((name, window), 0.0)


def _acct(pct=50.0, reset_min=500, **extra):
    a = {"name": "A", "capacity_x": 20, "current_5h_pct": pct}
    if reset_min is not None:
        a["five_hour_resets_at"] = _iso(NOW + timedelta(minutes=reset_min))
    a.update(extra)
    return a


# ------------------------------ _required_reduction_pool ----------------------

def test_pool_reduction_exact_at_knot():
    """Δ* = clamp(rotatable_burn - min[pool_remaining(t_k)/t_k], 0, rotatable_burn),
    the min taken exactly at a knot."""
    pool_curve = lambda t: 10.0 - 0.03 * t  # noqa: E731
    knots_rr = [0.0, 120.0, 240.0]
    # min remaining/t at t=240: (10-7.2)/240 = 0.011667
    out = cus._required_reduction_pool(pool_curve, 0.05, knots_rr, CFG, NOW)
    assert out["delta_units"] == pytest.approx(0.05 - (10.0 - 7.2) / 240.0, abs=1e-6)
    assert out["unmeetable"] is False


def test_no_breach_zero_reduction():
    """Healthy pool -> min-ratio >= rotatable_burn -> Δ* clamps to 0, meetable."""
    out = cus._required_reduction_pool(lambda t: 100.0, 0.1, [0.0, 120.0, 240.0], CFG, NOW)
    assert out["delta_units"] == 0.0
    assert out["unmeetable"] is False


def test_supply_breaches_unmeetable():
    """A knot where pool_remaining <= 0 (pinned drain alone gates) -> min-ratio <= 0
    -> unmeetable=True (§5.4)."""
    pool_curve = lambda t: 5.0 - 0.1 * t  # noqa: E731  (at 240: -19)
    out = cus._required_reduction_pool(pool_curve, 0.05, [0.0, 240.0], CFG, NOW)
    assert out["unmeetable"] is True
    assert out["delta_units"] == pytest.approx(0.05, abs=1e-6)  # clamped to rotatable


def test_reduction_min_at_240_knot():
    """The final-segment min lies at t=240 itself, not any knot <= 180 -> K_rr
    must include 240 (round-1 finding). Clearing only to 180 would under-throttle."""
    pool_curve = lambda t: 10.0 - 0.03 * t  # noqa: E731  monotone decreasing
    d240 = cus._required_reduction_pool(pool_curve, 0.05, [0.0, 120.0, 240.0], CFG, NOW)
    d180 = cus._required_reduction_pool(pool_curve, 0.05, [0.0, 120.0, 180.0], CFG, NOW)
    # 240-knot min-ratio (0.011667) < 180-knot (0.025556) -> larger, correct Δ*.
    assert d240["delta_units"] == pytest.approx(0.05 - (10.0 - 7.2) / 240.0, abs=1e-6)
    assert d240["delta_units"] > d180["delta_units"]


def test_reduction_interior_clamp_knot():
    """The min-ratio lies at a Z_rr clamp-zero (interior kink) knot, not a reset
    knot or endpoint (round-1 finding). Dropping the Z knot under-throttles."""
    def pool_curve(t):
        if t <= 100.0:
            return 12.0 - 0.10 * t      # 0->12, 100->2.0  (decreasing to kink)
        return 2.0 + 0.05 * (t - 100.0)  # 100->2.0, 240->9.0 (rising: min at kink)
    with_z = cus._required_reduction_pool(pool_curve, 0.05, [0.0, 100.0, 240.0], CFG, NOW)
    without_z = cus._required_reduction_pool(pool_curve, 0.05, [0.0, 240.0], CFG, NOW)
    # interior kink min-ratio = 2.0/100 = 0.02
    assert with_z["delta_units"] == pytest.approx(0.05 - 2.0 / 100.0, abs=1e-6)
    # without the Z knot the min falls at 240 (9/240=0.0375) -> smaller, unsafe Δ*.
    assert with_z["delta_units"] > without_z["delta_units"]


# ------------------------------ _required_reduction_pinned --------------------

def test_pinned_reduction_pct_units():
    """Per-account reduction is returned in %/min (h_a = gate-pct headroom),
    matching the raw % headroom formula."""
    acct = _acct()  # pct=50, gate 94, ratio 4.0, no reset in range
    part = FakePartition(pinned={("A", "5h"): 0.005})  # units/min
    out = cus._required_reduction_pinned(acct, "5h", CFG, NOW, part)
    # % formula: pinned_pct = 0.005*100/4 = 0.125; h_a(t)=(94-50)-0.125t
    pinned_pct = 0.005 * 100.0 / RATIO
    min_ratio_pct = (44.0 - 0.125 * 240.0) / 240.0  # min at t=240
    expected = pinned_pct - min_ratio_pct
    assert out["delta_pct_per_min"] == pytest.approx(expected, abs=1e-4)
    assert out["unmeetable"] is False


def test_pinned_clear_to_H_plus_margin():
    """The pinned min is taken over knots up to 240 (not 180): clearing to 240
    yields a strictly larger Δ* than clearing only to 180 would."""
    acct = _acct()
    part = FakePartition(pinned={("A", "5h"): 0.005})
    out = cus._required_reduction_pinned(acct, "5h", CFG, NOW, part)
    # min-ratio at 240 is smaller than at 180 -> Δ* bigger than a 180-clear.
    pinned_pct = 0.005 * 100.0 / RATIO
    d240 = pinned_pct - (44.0 - 0.125 * 240.0) / 240.0
    d180 = pinned_pct - (44.0 - 0.125 * 180.0) / 180.0
    assert d240 > d180
    assert out["delta_pct_per_min"] == pytest.approx(d240, abs=1e-4)


def test_reset_in_180_240_credited():
    """A 5h reset at T_w=200: the min-ratio/Δ* CREDIT that reset's ramp over
    (200,240] (horizon=240 curve/knots), so Δ* is strictly smaller than the
    180-capped burn-through over-estimate that drops the reset (round-2 finding 1)."""
    part = FakePartition(pinned={("A", "5h"): 0.006})
    credited = cus._required_reduction_pinned(_acct(reset_min=200), "5h", CFG, NOW, part)
    # reset beyond 240 -> no ramp credit in [0,240] == the 180-capped burn-through.
    burn_through = cus._required_reduction_pinned(_acct(reset_min=500), "5h", CFG, NOW, part)
    assert credited["unmeetable"] is False
    assert burn_through["unmeetable"] is False
    assert credited["delta_pct_per_min"] < burn_through["delta_pct_per_min"]


def test_pinned_unmeetable_when_supply_breaches():
    """When pinned drain alone drives the account's own headroom <= 0 at a knot,
    min-ratio <= 0 -> unmeetable=True (§5.4)."""
    acct = _acct(pct=93.0)  # remaining0 = (94-93)/100*4 = 0.04 units, tiny
    part = FakePartition(pinned={("A", "5h"): 0.01})  # drains through 0 within 240
    out = cus._required_reduction_pinned(acct, "5h", CFG, NOW, part)
    assert out["unmeetable"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
