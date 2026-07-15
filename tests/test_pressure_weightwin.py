"""Task 14 (spec-2 token-pressure forecaster, STAGE 1): weight-fit window
builder -- ``_build_weight_windows`` assembles the regression inputs
``(A, b)`` for the burn-weight NNLS fit (Task 15/16), ONE row per
attribution window. This function IS the G4 attribution boundary: all
exclusions/corrections/normalization happen HERE so the solver receives a
clean ``(A, b)`` and does no filtering of its own.

Input shapes (this task's design -- no earlier task defines them; the real
wiring lands at Task 20):

* ``pct_history[i]`` -- one candidate attribution window (the interval
  between two consecutive usage polls for ONE account)::

      {"account": str, "start_ts": iso8601, "end_ts": iso8601,
       "pct_start": float, "pct_end": float, "ratio": float}

  ``ratio`` is the PRE-RESOLVED ``capacity_x / reference_x`` tier factor
  (Task 2 ``_pressure_ratio``/``_pressure_acct_ratio``) -- this function
  takes no ``config``, so the caller resolves ratio upstream.

* ``token_totals_per_window[i]`` -- index-aligned with ``pct_history``, the
  raw token totals accumulated during that window's interval::

      {"input": num, "output": num, "cache_read": num,
       "cache_create_5m": num, "cache_create_1h": num}

* ``resets`` -- observed reset-boundary crossings, NOT index-aligned (an
  account may have 0+ crossings during any given window's interval)::

      [{"account": str, "ts": iso8601}, ...]

HARNESS (same as every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Run with ``python -m pytest tests/ -q``.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _window(account="acct1", start=NOW, end=None, pct_start=10.0, pct_end=15.0,
            ratio=1.0, minutes=5):
    if end is None:
        end = start + timedelta(minutes=minutes)
    return {
        "account": account,
        "start_ts": _iso(start),
        "end_ts": _iso(end),
        "pct_start": pct_start,
        "pct_end": pct_end,
        "ratio": ratio,
    }


def _totals(input=10.0, output=10.0, cache_read=10.0, cache_create_5m=10.0,
            cache_create_1h=10.0):
    return {
        "input": input,
        "output": output,
        "cache_read": cache_read,
        "cache_create_5m": cache_create_5m,
        "cache_create_1h": cache_create_1h,
    }


def test_reset_crossing_window_excluded():
    """A window whose interval crosses a reset boundary for its account is
    dropped entirely (Δburn across the boundary isn't measurable -- the %
    dropped at reset) and counted under "reset_crossing"."""
    win = _window(start=NOW, end=NOW + timedelta(minutes=10), pct_start=80.0, pct_end=5.0)
    totals = _totals()
    resets = [{"account": "acct1", "ts": _iso(NOW + timedelta(minutes=5))}]

    A, b, dropped = cus._build_weight_windows([win], [totals], resets)

    assert A == []
    assert b == []
    assert dropped == {"reset_crossing": 1}


def test_reset_crossing_only_drops_matching_account_and_interval():
    """A reset for a DIFFERENT account, or outside this window's interval,
    does not exclude the window."""
    win = _window(account="acct1", start=NOW, end=NOW + timedelta(minutes=10),
                  pct_start=10.0, pct_end=15.0, ratio=1.0)
    totals = _totals()
    resets = [
        {"account": "acct2", "ts": _iso(NOW + timedelta(minutes=5))},  # wrong account
        {"account": "acct1", "ts": _iso(NOW - timedelta(minutes=5))},  # before interval
        {"account": "acct1", "ts": _iso(NOW + timedelta(minutes=30))},  # after interval
    ]

    A, b, dropped = cus._build_weight_windows([win], [totals], resets)

    assert len(A) == 1
    assert len(b) == 1
    assert dropped == {}


def test_within_window_expiry_corrected():
    """cache_create_5m/1h are corrected by min(1, ttl_minutes/duration_minutes)
    -- the documented v1 "fraction of the window still live within its TTL"
    correction. A 10-minute window: cache_create_5m (ttl=5) is only live for
    half the window -> factor 0.5; cache_create_1h (ttl=60 > duration) is
    live for the whole window -> factor 1.0 (no correction, clamped)."""
    win = _window(start=NOW, end=NOW + timedelta(minutes=10), pct_start=10.0, pct_end=20.0)
    totals = _totals(input=1.0, output=1.0, cache_read=1.0,
                      cache_create_5m=100.0, cache_create_1h=60.0)

    A, b, dropped = cus._build_weight_windows([win], [totals], [])

    assert dropped == {}
    assert len(A) == 1
    row = A[0]
    assert row[3] == 50.0  # cache_create_5m: 100 * min(1, 5/10) = 50
    assert row[4] == 60.0  # cache_create_1h: 60 * min(1, 60/10) = 60 (clamped to 1.0)


def test_drop_zero_token_and_nonpositive_b():
    """An all-zero-token window is dropped ("zero_tokens"); a window with
    b<=0 (no measurable burn) is dropped ("nonpositive_b"). Both counted,
    neither appears in A/b."""
    zero_tok_win = _window(account="acct1", pct_start=10.0, pct_end=15.0, ratio=1.0)
    zero_tok_totals = _totals(0.0, 0.0, 0.0, 0.0, 0.0)

    nonpositive_b_win = _window(account="acct1", pct_start=15.0, pct_end=15.0, ratio=1.0)
    nonpositive_b_totals = _totals()

    A, b, dropped = cus._build_weight_windows(
        [zero_tok_win, nonpositive_b_win],
        [zero_tok_totals, nonpositive_b_totals],
        [],
    )

    assert A == []
    assert b == []
    assert dropped == {"zero_tokens": 1, "nonpositive_b": 1}


def test_b_tier_normalized():
    """b = (Δpct/100)*ratio -- equal ABSOLUTE burn (reference units) on a
    20x account (ratio=4.0) and a 5x account (ratio=1.0) yields EQUAL b,
    even though the raw %Δ differs 4x (the 20x account's meter moves 4x
    slower for the same absolute burn)."""
    win_20x = _window(account="big", pct_start=10.0, pct_end=12.0, ratio=4.0)   # Δpct=2
    win_5x = _window(account="small", pct_start=10.0, pct_end=18.0, ratio=1.0)  # Δpct=8
    totals = _totals()

    A, b, dropped = cus._build_weight_windows([win_20x, win_5x], [totals, totals], [])

    assert dropped == {}
    assert len(b) == 2
    assert b[0] == pytest.approx(0.08)
    assert b[1] == pytest.approx(0.08)
    assert b[0] == pytest.approx(b[1])


def test_column_order_pinned():
    """A rows are exactly [input, output, cache_read, cache_create_5m,
    cache_create_1h] -- PINNED order (Task 15/16 priors are keyed
    positionally). Window duration (1 min) is below both TTLs so no expiry
    correction is applied, isolating the column-order check."""
    win = _window(start=NOW, end=NOW + timedelta(minutes=1), pct_start=10.0, pct_end=20.0)
    totals = _totals(input=1.0, output=2.0, cache_read=3.0,
                      cache_create_5m=4.0, cache_create_1h=5.0)

    A, b, dropped = cus._build_weight_windows([win], [totals], [])

    assert dropped == {}
    assert A == [[1.0, 2.0, 3.0, 4.0, 5.0]]


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
