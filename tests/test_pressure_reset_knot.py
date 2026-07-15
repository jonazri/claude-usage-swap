"""Task 3 (spec-2 token-pressure forecaster, STAGE 1): reset knots with stale
``five_hour_resets_at`` roll-forward (reset-decay risk #2).

``_pressure_reset_knot`` returns ``T_w`` = minutes to the NEXT 5h/7d reset,
self-rolling a stale ``five_hour_resets_at`` forward by whole ``W5=300`` min
periods so ``T_w`` is never negative (never a false immediate post-reset
credit). Returns the offset only when ``0 < T_w <= horizon``, else ``None``.

The ``horizon`` parameter DECOUPLES ``T_w`` from a hardcoded 180 (finding 1):
BOTH the trigger/exit-ETA path and the required-reduction path pass
``horizon = H+margin = 240``; ``H=180`` is only the ENTER threshold applied
downstream. Without the 240 knot a 5h reset in ``(180, 240]`` is dropped.

HARNESS: import cus as a module; run ``python -m pytest tests/ -q``.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def test_5h_future_boundary():
    """A 5h boundary 40 min out -> 40.0 (minutes to the reset)."""
    acct = {"five_hour_resets_at": _iso(NOW + timedelta(minutes=40)), "current_5h_pct": 50.0}
    assert cus._pressure_reset_knot(acct, "5h", {}, NOW, horizon=180) == 40.0


def test_5h_stale_rolls_forward():
    """A STALE (past) 5h boundary rolls forward +W5(300) multiples: a reset 10
    min in the past becomes +290, NOT a negative / immediate-credit knot
    (reset-decay risk #2)."""
    acct = {"five_hour_resets_at": _iso(NOW - timedelta(minutes=10)), "current_5h_pct": 50.0}
    assert cus._pressure_reset_knot(acct, "5h", {}, NOW, horizon=300) == 290.0


def test_7d_uses_projected(monkeypatch):
    """7d delegates to projected_seven_day_reset @4587 (already self-rolled)."""
    monkeypatch.setattr(
        cus, "projected_seven_day_reset",
        lambda acct, config, now: _iso(NOW + timedelta(minutes=120)),
    )
    assert cus._pressure_reset_knot({}, "7d", {}, NOW, horizon=180) == 120.0


def test_beyond_horizon_none():
    """A reset past the horizon returns None (no knot inside [0, horizon])."""
    acct = {"five_hour_resets_at": _iso(NOW + timedelta(minutes=500)), "current_5h_pct": 50.0}
    assert cus._pressure_reset_knot(acct, "5h", {}, NOW, horizon=180) is None


def test_horizon_param_keeps_reset_in_180_240():
    """A reset at now+200: dropped (None) under horizon=180 but retained (200.0)
    under horizon=240 — the required-reduction / exit-ETA path keeps a 5h reset
    in the (180, 240] exit-margin band (finding 1)."""
    acct = {"five_hour_resets_at": _iso(NOW + timedelta(minutes=200)), "current_5h_pct": 50.0}
    assert cus._pressure_reset_knot(acct, "5h", {}, NOW, horizon=180) is None
    assert cus._pressure_reset_knot(acct, "5h", {}, NOW, horizon=240) == 200.0


def test_5h_missing_boundary_none():
    """No parseable five_hour_resets_at -> None (no knot to anchor)."""
    assert cus._pressure_reset_knot({"current_5h_pct": 50.0}, "5h", {}, NOW, horizon=180) is None


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
