"""Task 5 (spec-2 token-pressure forecaster, STAGE 1): pool remaining curve —
staggered-reset composition + pinned-burn subtraction (C1/§10.12, G6).

Sum the per-account decayed-reset curves (Task 4) into ONE pool curve, each
account contributing its OWN reset knot, draining supply by projected PINNED
burn while pool DEMAND counts ROTATABLE burn only (§3 hazard-b). Because each
per-account term reuses Task 4's exact ramp + constant credit
``R_a = C_a - remaining_a(0)``, ``pool_remaining(t) = Σ clamp(remaining_a(t), 0,
C_a)`` is provably the pointwise sum of the per-account curves (G6).

SEQUENCING (finding 2): the rotatable/pinned split is the Phase-D
``_partition_burn`` product (Task 11), wired at Task 20. These Phase-F helpers
take it as an INJECTED ``partition`` parameter, so the tests pass a synthetic
double exposing ``pinned_burn_units(name, window)`` / ``rotatable_burn_units(
name, window)`` in units/min.

HARNESS: import cus as a module; run ``python -m pytest tests/ -q``.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
W5 = 300.0
RATIO = 4.0

CFG = {
    "capacity_aware": {"reference_x": 5},
    "thresholds": {"steps": [70, 85, 94]},   # gate_5h = 94
    "accounts": [
        {"name": "A", "capacity_x": 20},
        {"name": "B", "capacity_x": 20},
    ],
}


def _iso(dt):
    return dt.isoformat()


class FakePartition:
    """Synthetic Task-11 ``_partition_burn`` double (finding 2): per-account
    disjoint pinned vs rotatable burn in units/min, keyed by (name, window)."""

    def __init__(self, pinned=None, rotatable=None):
        self._pinned = dict(pinned or {})
        self._rotatable = dict(rotatable or {})

    def pinned_burn_units(self, name, window):
        return self._pinned.get((name, window), 0.0)

    def rotatable_burn_units(self, name, window):
        return self._rotatable.get((name, window), 0.0)


def _state(a_reset=40, b_reset=200, a_pct=50.0, b_pct=50.0):
    return {"accounts": {
        "A": {"capacity_x": 20, "current_5h_pct": a_pct,
              "five_hour_resets_at": _iso(NOW + timedelta(minutes=a_reset))},
        "B": {"capacity_x": 20, "current_5h_pct": b_pct,
              "five_hour_resets_at": _iso(NOW + timedelta(minutes=b_reset))},
    }}


def _cap():
    return cus._pressure_cap_units(94.0, RATIO)


def _pointwise_expected(state, part, horizon, t):
    """Independent recomputation of Σ clamp(per-account curve, 0, C_a)."""
    cap = _cap()
    total = 0.0
    for name in cus._pressure_pool_set(state, "5h", CFG):
        acct = state["accounts"][name]
        pinned = part.pinned_burn_units(name, "5h")
        c = cus._pressure_remaining_curve(acct, "5h", CFG, NOW, pinned, horizon=horizon)
        total += max(0.0, min(cap, c(t)))
    return total


# ----------------------------- pointwise sum ----------------------------------

def test_pool_is_pointwise_sum():
    """pool_remaining(t) is exactly Σ clamp(remaining_a(t), 0, C_a) (G6)."""
    state = _state()
    part = FakePartition(pinned={("A", "5h"): 0.005, ("B", "5h"): 0.008})
    f = cus._pool_remaining_curve(state, "5h", CFG, NOW, part, horizon=240)
    for t in (0.0, 50.0, 100.0, 150.0, 220.0):
        assert f(t) == _pointwise_expected(state, part, 240, t)


def test_staggered_resets_distinct_knots():
    """Each account lifts ONLY its own contribution at its own knot: at t=100 A
    (reset 40) has ramp credit, B (reset 200) has none."""
    state = _state(a_reset=40, b_reset=200)
    part = FakePartition(pinned={("A", "5h"): 0.005, ("B", "5h"): 0.005})
    cap = _cap()
    remaining0 = cus._pressure_remaining_units(50.0, 94.0, RATIO)

    cA = cus._pressure_remaining_curve(state["accounts"]["A"], "5h", CFG, NOW,
                                       0.005, horizon=240)
    cB = cus._pressure_remaining_curve(state["accounts"]["B"], "5h", CFG, NOW,
                                       0.005, horizon=240)
    # A already past its boundary -> ramp credit; B not yet -> pure burn-down.
    assert cA(100.0) > remaining0 - 0.005 * 100.0
    assert cB(100.0) == remaining0 - 0.005 * 100.0

    f = cus._pool_remaining_curve(state, "5h", CFG, NOW, part, horizon=240)
    assert f(100.0) == max(0.0, min(cap, cA(100.0))) + max(0.0, min(cap, cB(100.0)))


# ------------------------- pinned subtraction (conservative) ------------------

def test_pinned_subtraction_conservative():
    """Adding projected pinned burn drains pool SUPPLY -> the curve is lower
    everywhere (t>0), so any first-crossing ETA is only EARLIER (conservative)."""
    state = _state()
    with_pinned = FakePartition(pinned={("A", "5h"): 0.02, ("B", "5h"): 0.02})
    no_pinned = FakePartition(pinned={("A", "5h"): 0.0, ("B", "5h"): 0.0})
    f_pin = cus._pool_remaining_curve(state, "5h", CFG, NOW, with_pinned, horizon=240)
    f_nop = cus._pool_remaining_curve(state, "5h", CFG, NOW, no_pinned, horizon=240)
    for t in (50.0, 100.0, 150.0):
        assert f_pin(t) < f_nop(t)


# ------------------------- rotatable-only demand ------------------------------

def test_rotatable_only_demand():
    """rotatable_burn sums ONLY the rotatable component; the pinned share is
    excluded (it feeds the per-account safety floor, Task 7)."""
    state = _state()
    part = FakePartition(
        pinned={("A", "5h"): 0.05, ("B", "5h"): 0.07},
        rotatable={("A", "5h"): 0.03, ("B", "5h"): 0.04},
    )
    assert cus._pool_rotatable_burn(state, "5h", CFG, NOW, part) == 0.03 + 0.04


def test_gated_acct_absent():
    """An account at/above the gate is in neither the pool curve nor the
    rotatable-burn demand (excluded by _pressure_pool_set)."""
    cfg = dict(CFG, accounts=CFG["accounts"] + [{"name": "G", "capacity_x": 20}])
    state = _state()
    state["accounts"]["G"] = {"capacity_x": 20, "current_5h_pct": 96.0,
                              "five_hour_resets_at": _iso(NOW + timedelta(minutes=40))}
    part = FakePartition(
        rotatable={("A", "5h"): 0.03, ("B", "5h"): 0.04, ("G", "5h"): 9.0},
    )
    # G's huge rotatable burn must NOT be counted.
    assert cus._pool_rotatable_burn(state, "5h", cfg, NOW, part) == 0.03 + 0.04


def test_partition_injected_not_from_state():
    """Both helpers read the INJECTED partition, never the account-total
    state.burn_rate (finding 2): a partition with zero burn yields zero demand
    and no supply drain even though state carries a large burn_rate."""
    state = _state()
    for name in ("A", "B"):
        state["accounts"][name]["burn_rate_5h_pct_per_min"] = 5.0  # huge, ignored
    empty = FakePartition()  # zero pinned + zero rotatable
    assert cus._pool_rotatable_burn(state, "5h", CFG, NOW, empty) == 0.0
    f = cus._pool_remaining_curve(state, "5h", CFG, NOW, empty, horizon=240)
    # pinned=0 -> no supply drain; f(t) never dips below its reset-only value.
    r0 = cus._pressure_remaining_units(50.0, 94.0, RATIO)
    assert f(10.0) == 2 * r0  # two accounts, no burn yet, before either boundary


# ------------------------- horizon 240 credits late reset ---------------------

def test_horizon_240_credits_late_reset():
    """A reset at T_w=200 lifts pool_remaining(t) for t in (200,240] under
    horizon=240, but is dropped under horizon=180 (finding 1)."""
    state = {"accounts": {
        "B": {"capacity_x": 20, "current_5h_pct": 50.0,
              "five_hour_resets_at": _iso(NOW + timedelta(minutes=200))},
    }}
    cfg = dict(CFG, accounts=[{"name": "B", "capacity_x": 20}])
    part = FakePartition(pinned={("B", "5h"): 0.001})
    f240 = cus._pool_remaining_curve(state, "5h", cfg, NOW, part, horizon=240)
    f180 = cus._pool_remaining_curve(state, "5h", cfg, NOW, part, horizon=180)
    assert f240(220.0) > f180(220.0)
    r0 = cus._pressure_remaining_units(50.0, 94.0, RATIO)
    R_w = _cap() - r0
    assert f240(220.0) - f180(220.0) == R_w * (20.0 / W5)


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
