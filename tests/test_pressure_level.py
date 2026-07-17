"""Task 9 (spec-2 token-pressure STAGE 1): level model, most-severe/tie, over-gate
short-circuit, pool-release suppression (§3, C1/I1/#6, G6).

Fold the independent triggers ``{pool:5h, pool:7d} ∪ {per-acct:5h/7d ∀a}`` into
one ``ok/elevated/critical`` level. Each trigger's breach ETA is computed to
``horizon = H+margin = 240`` (round-3 finding 1) so the EXIT test is evaluable in
``(180, 240]``: a trigger breaching there keeps a REAL eta (not None) and is held
binding. ``_pressure_level`` is STATELESS (no memory of the prior cycle's level),
so eta<=240 -- NOT the plan's 180 -- is the only threshold it ever applies, for
BOTH entering and holding binding: critical at ``eta < 60`` or ``level_bound``;
elevated for ANY OTHER binding trigger, i.e. up to ``eta == 240``. A trigger
clears only when its 240-horizon eta exceeds 240 (i.e. eta is None). The plan's
true hysteresis -- ENTER at the tighter ``H = 180``, EXIT only at 240 after an
``exit_dwell_min`` dwell -- needs prior-cycle level STATE this stateless fold does
not have, and is DEFERRED to the Stage-2 acting path (pre-shadow-flip gate); see
`cus._pressure_level`'s docstring. Fable-weekly binds by LEVEL. pool↔per-account
tie -> per-account WINS. Pool release is suppressed while any active account is
unpolled / capacity_x out-of-band.

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
    "thresholds": {"steps": [70, 85, 94]},           # gate_5h = 94
    "per_model_weekly": {"cap_pct": 95},
    "pressure": {"critical_eta_min": 60, "weekly_gate_margin_pct": 2,
                 "horizon_hours": 3},
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


def _pool(window="5h", eta=None, suppressed=False):
    return {"view": "pool", "window": window, "eta": eta,
            "binding_constraint": f"token-pressure:pool:{window}",
            "release_suppressed": suppressed}


def _acct_trig(name="A", window="5h", eta=None, level_bound=False):
    return {"view": "account", "window": window, "eta": eta,
            "binding_constraint": f"token-pressure:account:{name}:{window}",
            "level_bound": level_bound}


# ============================ _pressure_level / _pressure_binding =============
# These operate on trigger lists directly (the level model's core logic).

def test_most_severe_across_disjoint():
    """pool elevated + per-account critical -> critical, binding = account."""
    triggers = [_pool(eta=120.0), _acct_trig(eta=30.0)]
    out = cus._pressure_level(triggers, CFG)
    assert out["level"] == "critical"
    assert out["binding"]["view"] == "account"


def test_pool_never_masks_pinned():
    """A healthy pool (no pool trigger) never hides a per-account pinned breach."""
    triggers = [_acct_trig(eta=30.0)]
    out = cus._pressure_level(triggers, CFG)
    assert out["level"] == "critical"
    assert out["binding"]["view"] == "account"


def test_tie_per_account_wins():
    """pool and per-account with the SAME eta -> per-account WINS the binding."""
    triggers = [_pool(eta=100.0), _acct_trig(eta=100.0)]
    out = cus._pressure_level(triggers, CFG)
    assert out["binding"]["view"] == "account"


def test_exit_requires_all_binding_clear():
    """Level clears to ok only when EVERY binding trigger's 240-horizon eta
    exceeds 240 (eta is None). One still-binding trigger holds elevated
    (committee #6)."""
    assert cus._pressure_level([_acct_trig(eta=None), _pool(eta=None)], CFG)["level"] == "ok"
    assert cus._pressure_level([_acct_trig(eta=None), _acct_trig(eta=100.0)], CFG)["level"] == "elevated"


def test_exit_binding_in_180_240_band():
    """A binding trigger whose breach recedes to t=200 keeps a real 240-horizon
    eta and MUST NOT decrease the level to ok — exit clears only at eta>240
    (None) (round-3 finding 1)."""
    out = cus._pressure_level([_acct_trig(eta=200.0)], CFG)
    assert out["level"] == "elevated"
    assert out["binding"]["eta"] == 200.0


def test_fable_level_bound_is_critical():
    """A level_bound (Fable-weekly) trigger is critical regardless of eta and
    carries no numeric reduction."""
    out = cus._pressure_level([_acct_trig(window="7d", eta=0.0, level_bound=True)], CFG)
    assert out["level"] == "critical"
    assert out["binding"]["level_bound"] is True


# ============================ _pressure_triggers integration =================

def _state(accounts):
    return {"accounts": accounts}


def test_over_gate_short_circuit(monkeypatch):
    """pct >= gate -> immediate eta 0.0 (root-find skipped) -> critical."""
    monkeypatch.setattr(cus, "projected_seven_day_reset", lambda a, c, n: None)
    cfg = dict(CFG, accounts=[{"name": "A", "capacity_x": 20}])
    state = _state({"A": {"capacity_x": 20, "current_5h_pct": 96.0,
                          "last_poll_ts": _iso(NOW)}})
    part = FakePartition(pinned={("A", "5h"): 0.001})
    triggers = cus._pressure_triggers(state, cfg, NOW, part)
    lvl = cus._pressure_level(triggers, cfg)
    assert lvl["level"] == "critical"
    binding = lvl["binding"]
    assert binding["view"] == "account" and binding["eta"] == 0.0


def test_fable_level_bound_critical(monkeypatch):
    """Fable at 94 with cap 95, margin 2 -> level_bound critical, no numeric
    reduction (FACT #7, G8)."""
    monkeypatch.setattr(cus, "projected_seven_day_reset", lambda a, c, n: None)
    cfg = dict(CFG, accounts=[{"name": "A", "capacity_x": 20}])
    state = _state({"A": {"capacity_x": 20, "current_5h_pct": 50.0,
                          "last_poll_ts": _iso(NOW),
                          "per_model_weekly_pct": {"Fable": 94.0}}})
    part = FakePartition()  # zero burn -> no per-account/pool ETA trigger
    triggers = cus._pressure_triggers(state, cfg, NOW, part)
    fable = [t for t in triggers if t.get("level_bound")]
    assert len(fable) == 1
    lvl = cus._pressure_level(triggers, cfg)
    assert lvl["level"] == "critical"
    assert lvl["binding"]["level_bound"] is True


def test_unpolled_suppresses_pool_release(monkeypatch):
    """An unpolled active account suppresses pool-driven release: a healthy pool
    stays binding (elevated) instead of clearing to ok (C1/I4)."""
    monkeypatch.setattr(cus, "projected_seven_day_reset", lambda a, c, n: None)
    cfg = dict(CFG, accounts=[{"name": "A", "capacity_x": 20},
                              {"name": "B", "capacity_x": 20}])
    # A polled & healthy; B active but NEVER polled -> pool view uncertain.
    unpolled = _state({
        "A": {"capacity_x": 20, "current_5h_pct": 50.0, "last_poll_ts": _iso(NOW)},
        "B": {"capacity_x": 20, "current_5h_pct": 50.0},  # no last_poll_ts
    })
    part = FakePartition()  # zero burn everywhere -> pool genuinely healthy
    assert cus._pool_release_suppressed(unpolled, cfg) is True
    triggers = cus._pressure_triggers(unpolled, cfg, NOW, part)
    lvl = cus._pressure_level(triggers, cfg)
    assert lvl["level"] == "elevated"
    assert lvl["binding"]["view"] == "pool"

    # Contrast: both polled + healthy -> no suppression -> ok.
    polled = _state({
        "A": {"capacity_x": 20, "current_5h_pct": 50.0, "last_poll_ts": _iso(NOW)},
        "B": {"capacity_x": 20, "current_5h_pct": 50.0, "last_poll_ts": _iso(NOW)},
    })
    assert cus._pool_release_suppressed(polled, cfg) is False
    triggers2 = cus._pressure_triggers(polled, cfg, NOW, part)
    assert cus._pressure_level(triggers2, cfg)["level"] == "ok"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
