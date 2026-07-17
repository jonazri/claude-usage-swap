"""Task 7 (spec-2 token-pressure STAGE 1): per-account pinned-burn ETA — the
safety floor (C1/M1, G6).

Forecast when an account's PINNED (non-rotatable) burn ALONE reaches its own
gate, on the raw observed rate (rotation-blind), a disjoint population that fires
independently of pool health. Burn comes from the INJECTED Task-11
``_partition_burn`` partition (finding 2 — Phase F cannot reach the per-session
split via ``state``); tests inject a synthetic double keyed by (name, window) in
units/min, exactly the Task-5 protocol.

HARNESS: ``python3 -m pytest tests/ -q``.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
RATIO = 4.0  # capacity_x 20 / reference_x 5
CFG = {
    "capacity_aware": {"reference_x": 5},
    "thresholds": {"steps": [70, 85, 94]},  # gate_5h = 94
}


def _iso(dt):
    return dt.isoformat()


class FakePartition:
    """Synthetic Task-11 ``_partition_burn`` double: disjoint pinned vs rotatable
    burn in units/min, keyed by (name, window) — the Task-5 protocol."""

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


def _remaining0(pct=50.0):
    return cus._pressure_remaining_units(pct, 94.0, RATIO)  # (94-50)/100*4 = 1.76


# ------------------------------ _pinned_burn_rate -----------------------------

def test_pinned_burn_from_injected_partition():
    """`_pinned_burn_rate` reads the INJECTED partition, NEVER the account-total
    state.burn_rate (finding 2)."""
    acct = _acct(burn_rate_5h_pct_per_min=5.0)  # huge account-total, must be ignored
    part = FakePartition(pinned={("A", "5h"): 0.02})
    assert cus._pinned_burn_rate(acct, "5h", part) == 0.02


# ------------------------------ _pinned_account_eta ---------------------------

def test_pinned_independent_of_healthy_pool():
    """The pinned floor is computed from the account's OWN curve alone — no pool
    input — so it fires independently of a healthy pool. remaining0/pinned."""
    acct = _acct()  # remaining0 = 1.76, reset beyond horizon -> no ramp
    part = FakePartition(pinned={("A", "5h"): 0.02})
    eta = cus._pinned_account_eta(acct, "5h", CFG, NOW, part, horizon=180)
    assert eta == pytest.approx(_remaining0() / 0.02, abs=0.5)  # 88.0


def test_raw_rate_not_discounted():
    """Uses the raw injected pinned rate, never a 'will rotate' discount: doubling
    the raw rate halves the ETA."""
    acct = _acct()
    slow = cus._pinned_account_eta(acct, "5h", CFG, NOW,
                                   FakePartition(pinned={("A", "5h"): 0.02}), horizon=180)
    fast = cus._pinned_account_eta(acct, "5h", CFG, NOW,
                                   FakePartition(pinned={("A", "5h"): 0.04}), horizon=180)
    assert slow == pytest.approx(88.0, abs=0.5)
    assert fast == pytest.approx(44.0, abs=0.5)


def test_over_gate_immediate():
    """pct >= gate -> remaining0 = 0 -> g(0) <= 0 -> immediate 0.0."""
    acct = _acct(pct=96.0)
    part = FakePartition(pinned={("A", "5h"): 0.02})
    assert cus._pinned_account_eta(acct, "5h", CFG, NOW, part, horizon=180) == 0.0


def test_disjoint_no_double_count():
    """The pinned ETA uses ONLY the pinned component; the rotatable component is
    NOT added (disjoint populations, §3/C1)."""
    acct = _acct()
    part = FakePartition(pinned={("A", "5h"): 0.02}, rotatable={("A", "5h"): 9.0})
    eta = cus._pinned_account_eta(acct, "5h", CFG, NOW, part, horizon=180)
    assert eta == pytest.approx(88.0, abs=0.5)  # from 0.02 only, not 0.02+9.0


def test_pinned_eta_horizon_surfaces_180_240_band():
    """A pinned breach at t=200 is None under horizon=180 but 200.0 under
    horizon=240, so the trigger/exit path can see it receding (round-3 finding 1).
    A within-180 breach would be identical at either cap."""
    r0 = _remaining0()
    pinned = r0 / 200.0  # crossing exactly at t=200
    acct = _acct(reset_min=None)  # no reset -> pure burn-down
    part = FakePartition(pinned={("A", "5h"): pinned})
    assert cus._pinned_account_eta(acct, "5h", CFG, NOW, part, horizon=180) is None
    eta240 = cus._pinned_account_eta(acct, "5h", CFG, NOW, part, horizon=240)
    assert eta240 == pytest.approx(200.0, abs=0.5)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
