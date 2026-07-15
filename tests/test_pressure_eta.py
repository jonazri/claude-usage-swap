"""Task 6 (spec-2 token-pressure STAGE 1): pool-exhaustion first-crossing ETA.

``_first_crossing_eta(remaining_curve, burn, knots, config, now)`` — the earliest
``t`` where the pool slack ``g(t) = remaining_curve(t) - burn*t`` hits 0, via a
stdlib segmented bracket-then-bisect over a CALLER-supplied knot set (NOT
``Σremaining/Σburn``, NOT scipy — committee C2, FACT #8, G6). Constants pinned:
``H=180`` (the caller caps knots at H+margin=240), ``TTE_TOL=0.5``,
``BISECT_MAX_ITERS=64``, breach predicate ``g(t) <= 0`` (``EPS=0``).

HARNESS: ``python3 -m pytest tests/ -q``.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
CFG = {"pressure": {}}


def test_monotone_single_crossing():
    """No reset -> g linear -> crossing at remaining(0)/burn."""
    # remaining constant supply 10.0, demand burn 0.1/min -> g(t)=10-0.1t -> 100.
    eta = cus._first_crossing_eta(lambda t: 10.0, 0.1, [0.0, 180.0], CFG, NOW)
    assert eta == pytest.approx(100.0, abs=0.5)


def test_reset_recross_returns_first():
    """g dips <=0 then a reset ramp lifts it back >0 -> return the FIRST crossing
    (risk #4 — the whole reason for knot anchoring)."""
    def curve(t):
        if t <= 40.0:
            return 10.0 - 0.3 * t          # 0->10, 40->-2   (crosses ~33.3)
        if t <= 100.0:
            return -2.0 + 0.2 * (t - 40.0)  # 40->-2, 100->+10 (ramp recovers)
        return 10.0 - 0.1 * (t - 100.0)
    eta = cus._first_crossing_eta(curve, 0.0, [0.0, 40.0, 100.0, 180.0], CFG, NOW)
    assert eta == pytest.approx(33.33, abs=0.5)


def test_interior_clamp_zero_recross():
    """One account floors at 0 mid-segment while another's ramp lifts g back >0.
    The ``Z`` clamp-zero knot catches the transient breach an endpoint-only
    bracket would miss (finding 1)."""
    def curve(t):
        if t <= 50.0:
            return 10.0 - 0.26 * t              # 0->10, 50->-3 (crosses ~38.5)
        if t <= 80.0:
            return -3.0 + (7.0 / 30.0) * (t - 50.0)  # 50->-3, 80->+4 (recovers)
        return 4.0 + 0.05 * (t - 80.0)               # keeps rising, stays >0
    # WITH the Z knot at 50 -> the dip is bracketed -> first crossing found.
    eta = cus._first_crossing_eta(curve, 0.0, [0.0, 50.0, 80.0, 180.0], CFG, NOW)
    assert eta == pytest.approx(38.46, abs=0.5)
    # WITHOUT the Z knot -> endpoint-only bracket (g(0)=10>0, g(80)=+4>0) MISSES
    # the transient breach -> unsafe under-forecast (None). This is the bug the
    # Z knots exist to prevent; the caller supplies them.
    assert cus._first_crossing_eta(curve, 0.0, [0.0, 80.0, 180.0], CFG, NOW) is None


def test_already_drained():
    """g(0) <= 0 -> ETA 0.0 (already breached)."""
    assert cus._first_crossing_eta(lambda t: 0.0, 0.1, [0.0, 180.0], CFG, NOW) == 0.0
    assert cus._first_crossing_eta(lambda t: -2.0, 0.0, [0.0, 180.0], CFG, NOW) == 0.0


def test_no_breach_in_horizon():
    """g > 0 across the whole supplied range -> None."""
    assert cus._first_crossing_eta(lambda t: 100.0, 0.1, [0.0, 180.0], CFG, NOW) is None


def test_tolerance_and_iter_cap():
    """Bisection returns within TTE_TOL of the analytic root and terminates."""
    # g(t) = 100 - t, root at 100.0.
    eta = cus._first_crossing_eta(lambda t: 100.0, 1.0, [0.0, 240.0], CFG, NOW)
    assert eta == pytest.approx(100.0, abs=0.5)
    assert abs(eta - 100.0) <= 0.5


def test_caller_cap_240_returns_crossing_in_180_240():
    """`_first_crossing_eta` is horizon-agnostic: a crossing at t=200 is returned
    when the caller supplies knots capped at 240 (round-3 finding 1)."""
    eta = cus._first_crossing_eta(lambda t: 20.0, 0.1, [0.0, 240.0], CFG, NOW)
    assert eta == pytest.approx(200.0, abs=0.5)


def test_no_numpy_import():
    """Module imports (and this ETA computes) with no numpy/scipy available
    (FACT #8 — pure stdlib)."""
    assert "numpy" not in sys.modules
    assert "scipy" not in sys.modules


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
