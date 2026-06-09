"""Tests for live 5h-window rollover detection (GH #59).

`_five_hour_rolled_since_poll` lets the statusline flag "this 5h window just
reset" the moment its live countdown elapses — without waiting for the next
poll to confirm — while NOT false-flagging an idle account that's been sitting
at 0% since a poll already observed the reset.

Run standalone:  python3 tests/test_reset_rollover.py
Or under pytest: pytest tests/test_reset_rollover.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_rolled_when_reset_passed_and_poll_is_stale():
    """Reset fired 5 min ago, last poll was 12 min ago (before the reset) →
    the stored % is pre-reset stale → flag the rollover."""
    now = _now()
    acct = {
        "five_hour_resets_at": _iso(now - timedelta(minutes=5)),
        "last_poll_ts": _iso(now - timedelta(minutes=12)),
        "current_5h_pct": 96,
    }
    assert cus._five_hour_rolled_since_poll(acct) is True


def test_not_rolled_for_idle_account_already_observed_zero():
    """Reset fired 70 min ago but we polled 3 min ago (after the reset) and saw
    0% → not stale, don't flag."""
    now = _now()
    acct = {
        "five_hour_resets_at": _iso(now - timedelta(minutes=70)),
        "last_poll_ts": _iso(now - timedelta(minutes=3)),
        "current_5h_pct": 0,
    }
    assert cus._five_hour_rolled_since_poll(acct) is False


def test_not_rolled_when_reset_in_future():
    now = _now()
    acct = {
        "five_hour_resets_at": _iso(now + timedelta(hours=2)),
        "last_poll_ts": _iso(now - timedelta(minutes=3)),
        "current_5h_pct": 50,
    }
    assert cus._five_hour_rolled_since_poll(acct) is False


def test_not_rolled_when_no_reset_timestamp():
    assert cus._five_hour_rolled_since_poll({"current_5h_pct": 0}) is False


def test_rolled_when_never_polled():
    """Reset is in the past and we have no last_poll_ts → can't have observed
    the post-reset state → treat as rolled/stale."""
    now = _now()
    acct = {"five_hour_resets_at": _iso(now - timedelta(minutes=2)), "current_5h_pct": 80}
    assert cus._five_hour_rolled_since_poll(acct) is True


def _run_all() -> int:
    import types
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
