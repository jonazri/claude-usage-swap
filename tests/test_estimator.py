"""Tests for the burn-rate estimator (C, 2026-06-23).

The estimator extrapolates an account's CURRENT usage from its last poll + a
measured %/min burn rate, so the picker's "is this target too full" judgment
isn't fooled by a stale-but-climbing number. Pins:
  - _compute_burn_rate: %/min from two polls; 0 on reset/invalid input.
  - estimate_window_pct: upward-only, clamped to 100, capped at
    max_extrapolation_minutes, and a no-op when disabled / no rate.
  - wiring: a target polled BELOW the saturation line but climbing fast is
    excluded by pick_swap_target (the estimator's value-add over polled-only).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402

CFG = {
    "strategy": "smart",
    "thresholds": {"five_hour": True, "seven_day": True, "steps": [80, 92]},
    "smart_strategy": {"hard_7d_cap_pct": 95, "allow_rate_limited_targets": True},
    "swap_hysteresis": {"enabled": True, "min_improvement_pct": 5,
                        "min_seconds_between_swaps": 3000},
    "never_swap_to_pct": 100,
    "estimator": {"enabled": True, "max_extrapolation_minutes": 10},
}


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# _compute_burn_rate
# --------------------------------------------------------------------------

def test_burn_rate_basic():
    t0 = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=10)
    # 30% -> 50% over 10 min = 2.0 %/min
    assert cus._compute_burn_rate(30.0, 50.0, _iso(t0), _iso(t1)) == 2.0


def test_burn_rate_zero_on_reset_drop():
    t0 = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=10)
    # usage dropped (window reset between polls) → not a meaningful rate
    assert cus._compute_burn_rate(90.0, 5.0, _iso(t0), _iso(t1)) == 0.0


def test_burn_rate_zero_on_missing_or_bad_input():
    assert cus._compute_burn_rate(None, 50.0, "x", "y") == 0.0
    assert cus._compute_burn_rate(30.0, 50.0, None, None) == 0.0
    t0 = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    assert cus._compute_burn_rate(30.0, 50.0, _iso(t0), _iso(t0)) == 0.0  # dt=0


# --------------------------------------------------------------------------
# estimate_window_pct
# --------------------------------------------------------------------------

def _acct(pct, rate, mins_ago):
    now = datetime.now(timezone.utc)
    return {"current_5h_pct": pct,
            "burn_rate_5h_pct_per_min": rate,
            "last_poll_ts": _iso(now - timedelta(minutes=mins_ago))}


def test_estimate_extrapolates_upward():
    # polled 80%, 2%/min, 5 min since poll → ~90%
    est = cus.estimate_window_pct(_acct(80.0, 2.0, 5), "5h", CFG)
    assert 89.0 <= est <= 91.0, est


def test_estimate_clamps_to_100():
    est = cus.estimate_window_pct(_acct(95.0, 5.0, 5), "5h", CFG)
    assert est == 100.0


def test_estimate_caps_extrapolation_window():
    # 60 min since poll but cap is 10 min → only 10 min of extrapolation counts
    est = cus.estimate_window_pct(_acct(50.0, 1.0, 60), "5h", CFG)
    assert 59.0 <= est <= 61.0, est  # 50 + 1*10, not 50 + 1*60


def test_estimate_noop_without_rate():
    assert cus.estimate_window_pct(_acct(50.0, 0.0, 5), "5h", CFG) == 50.0


def test_estimate_noop_when_disabled():
    cfg = {**CFG, "estimator": {"enabled": False}}
    assert cus.estimate_window_pct(_acct(50.0, 5.0, 5), "5h", cfg) == 50.0


# --------------------------------------------------------------------------
# Wiring into pick_swap_target — the value-add over polled-only Fix B
# --------------------------------------------------------------------------

def _live(pct, rate, mins_ago):
    now = datetime.now(timezone.utc)
    return {"current_5h_pct": pct, "current_7d_pct": 20.0,
            "burn_rate_5h_pct_per_min": rate, "next_swap_at_pct": 80,
            "last_swap_ts": None,
            "last_poll_ts": _iso(now - timedelta(minutes=mins_ago))}


def test_climbing_target_below_100_is_excluded():
    """merkos polled at 92% (under the 100 saturation line, so polled-only Fix B
    would allow it) but climbing 2%/min, polled 5 min ago → est ~102 → excluded;
    with only that candidate, the picker stays put."""
    accts = {"default": _live(20.0, 0.0, 1), "merkos": _live(92.0, 2.0, 5)}
    assert cus.pick_swap_target({"active": "default", "accounts": accts}, CFG) is None


def test_flat_target_below_100_still_pickable():
    """Same 92% poll but a FLAT burn rate → est == polled == 92 < 100 → merkos
    remains a valid target. Confirms the estimator only excludes genuine climbers."""
    accts = {"default": _live(20.0, 0.0, 1), "merkos": _live(92.0, 0.0, 5)}
    t = cus.pick_swap_target({"active": "default", "accounts": accts}, CFG)
    assert t is not None and t.name == "merkos"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
