"""Regression tests for the 7d-aware "one rule" swap logic (2026-06-12).

Incident (2026-06-11T21:04): with `thresholds.seven_day: false` the swap ladder
was blind to the weekly window, so the daemon swapped a session ONTO `default`
when default was 7d=90% / 5h=0%, then left it parked there at 91-92% 7d for ~6
hours (the ladder only watched 5h, which was idle). The only 7d-aware gate was a
single contested `hard_7d_cap_pct` number doing double duty.

The fix is ONE coherent rule: an account is "full" at >=90% on EITHER window.
Never swap onto a full account; swap away from a full active account; if all are
full, hold + SOS. Expressed as: `thresholds.seven_day: true` + the re-trip
target filter clamped to steps[0] (so a parked account whose ladder ratcheted to
96 while its 5h sat at 0% — never rolling, so never reset — is still seen as
full). These tests pin both halves.

Run standalone:  python3 tests/test_7d_aware_swap.py
Or under pytest: pytest tests/test_7d_aware_swap.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# A self-contained config expressing the one rule — independent of the operator's
# live ~/claude-accounts/config.yaml so the test is deterministic.
CFG = {
    "strategy": "smart",
    "thresholds": {"five_hour": True, "seven_day": True, "steps": [90, 96]},
    "smart_strategy": {
        "five_hour_headroom_weight": 0.6, "seven_day_headroom_weight": 0.4,
        "hard_7d_cap_pct": 95, "allow_rate_limited_targets": True,
        "burn_soon_weight": 1.0, "burn_window_hours": 2,
    },
    "swap_hysteresis": {"enabled": True, "min_improvement_pct": 5,
                        "min_seconds_between_swaps": 3000, "min_seconds_between_cap_swaps": 60},
    "usage_growth_gate": {"enabled": True, "min_delta_pct": 0.5},
    # lazy/defer/burn left at defaults; these tests probe the decide/pick core.
}


def _u(h5, h7):
    return cus.AccountUsage(five_hour=cus.UsageWindow(h5, None),
                            seven_day=cus.UsageWindow(h7, None))


def _a(h5, h7, nxt):
    # last_swap_ts=None so hysteresis never blocks — we test the pressure logic.
    return {"current_5h_pct": h5, "current_7d_pct": h7,
            "next_swap_at_pct": nxt, "last_swap_ts": None}


def _decide(active, accts, usage):
    return cus.decide_swap({"active": active, "accounts": accts}, CFG, usage)


def _pick(active, accts):
    return cus.pick_swap_target({"active": active, "accounts": accts}, CFG)


def test_full_account_excluded_as_target_despite_ratcheted_ladder():
    """The core hole: `default` is weekly-dead (7d=92) but its ladder ratcheted
    to next=96 while idle, so the OLD re-trip filter (threshold=own step 96)
    let 92 < 96 pass. Clamped to steps[0]=90, 92 >= 90 → excluded."""
    accts = {"merkos": _a(96, 34, 90), "default": _a(0, 92, 96), "03": _a(48, 10, 90)}
    t = _pick("merkos", accts)
    assert t is not None and t.name == "03", f"expected 03, got {t and t.name}"


def test_swaps_onto_freshest_not_weekly_dead():
    """Whole incident, 3 accounts: evict the 5h-maxed active account onto the
    genuinely-fresh 03, never onto weekly-dead default."""
    accts = {"merkos": _a(96, 34, 90), "default": _a(0, 92, 96), "03": _a(48, 10, 90)}
    d = _decide("merkos", accts, {"merkos": _u(96, 34), "default": _u(0, 92), "03": _u(48, 10)})
    assert d is not None and d.target == "03", f"expected swap->03, got {d}"


def test_weekly_dead_active_account_is_evicted():
    """The actual harm: a session sitting on a 7d=91% account must be moved off
    (ladder is now 7d-aware), not parked for hours."""
    accts = {"default": _a(2, 91, 90), "merkos": _a(58, 34, 90), "03": _a(48, 10, 90)}
    d = _decide("default", accts, {"default": _u(2, 91), "merkos": _u(58, 34), "03": _u(48, 10)})
    assert d is not None and d.target == "03", f"expected eviction->03, got {d}"


def test_no_5h_pingpong_churn():
    """The 06-08 regression that motivated seven_day:false in the first place:
    both accounts ~92% on 5h / ~12% on 7d. Re-enabling 7d must NOT reintroduce
    churn — the min-improvement gate holds (neither account improves on the
    other by 5pp)."""
    accts = {"default": _a(92, 12, 90), "merkos": _a(92, 12, 90)}
    d = _decide("default", accts, {"default": _u(92, 12), "merkos": _u(92, 12)})
    assert d is None, f"expected HOLD (no churn), got swap->{d and d.target}"


def test_low_usage_active_account_not_evicted():
    """A lightly-used active account (5h=48/7d=10) is below the 90 line on both
    windows → no eviction. (Guards 'don't swap away from a healthy account'.)"""
    accts = {"03": _a(48, 10, 90), "default": _a(0, 92, 96), "merkos": _a(58, 34, 90)}
    d = _decide("03", accts, {"03": _u(48, 10), "default": _u(0, 92), "merkos": _u(58, 34)})
    assert d is None, f"expected HOLD, got swap->{d and d.target}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
