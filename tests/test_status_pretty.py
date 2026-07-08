"""Tests for `cus status --pretty` (rich renderer) and the shared
`_account_row` derivation both renderers consume.

Spec: docs/plans/2026-07-07-status-pretty.md

Run standalone:  python3 tests/test_status_pretty.py
Or under pytest: pytest tests/test_status_pretty.py
"""

import json
import sys
import tempfile
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# --- _account_row: shared derivation ---------------------------------------


def test_account_row_flags_order_and_status_col():
    row = cus._account_row("a", {"token_stale": True, "rate_limited": True}, {}, 50)
    assert row["flags"] == ["RATE_LIMITED", "TOKEN_STALE"]
    assert row["status_col"] == "RATE_LIMITED,TOKEN_STALE"


def test_account_row_ok_when_no_flags():
    row = cus._account_row("a", {}, {}, 50)
    assert row["flags"] == []
    assert row["status_col"] == "ok"
    assert row["last"] == "never"


def test_account_row_disabled_comes_from_config():
    config = {"accounts": [{"name": "a", "disabled": True}, {"name": "b"}]}
    assert cus._account_row("a", {}, config, 50)["flags"] == ["DISABLED"]
    assert cus._account_row("b", {}, config, 50)["flags"] == []


def test_account_row_next_swap_only_when_off_first_step():
    assert cus._account_row("a", {"next_swap_at_pct": 75}, {}, 50)["next_swap_pct"] == 75
    assert cus._account_row("a", {"next_swap_at_pct": 50}, {}, 50)["next_swap_pct"] is None
    assert cus._account_row("a", {}, {}, 50)["next_swap_pct"] is None


def test_account_row_per_model_sorted_highest_first():
    acct = {"per_model_weekly_pct": {"sonnet": 10.0, "fable": 80.0}}
    assert cus._account_row("a", acct, {}, 50)["per_model"] == [("fable", 80.0), ("sonnet", 10.0)]


def test_account_row_est_only_on_divergence():
    # Deterministic despite now(): extrapolation is capped at
    # estimator.max_extrapolation_minutes (default 10), and the anchor ts is
    # far in the past — est = 50 + 2.0 * 10 = 70.
    acct = {
        "current_5h_pct": 50.0,
        "burn_rate_5h_pct_per_min": 2.0,
        "last_observed_ts": "2026-01-01T00:00:00Z",
    }
    row = cus._account_row("a", acct, {}, 50)
    assert row["est"] == {"5h": (70.0, 2.0)}
    # No burn rate measured → estimator falls back to polled → no est entry.
    assert cus._account_row("a", {"current_5h_pct": 50.0}, {}, 50)["est"] == {}


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
