"""Task 29 (spec-2 token-pressure forecaster, STAGE 1): golden-file replay.

Each scenario under tests/fixtures/replay/<scenario>/ is a scrubbed, minimal
fixture (synthetic account names, no real tokens/credentials) exercising one
non-trivial forecaster property end to end through `cus.replay_forecast`:

  heavy-rotation   -- one session rotated through 6 accounts (sessions.log
                       rotation rows) -> per-(account,session) attribution +
                       pool contribution summed across all 6 accounts.
  reset-crossing   -- two accounts with DIFFERENT five_hour_resets_at offsets
                       inside the horizon -> pool ETA is the first crossing
                       over the STAGGERED reset knots, not a naive
                       Sum(remaining)/Sum(burn) estimate.
  mixed-tier       -- a 20x (ratio 4.0) and a 5x (ratio 1.0) account -> pool
                       capacity = Sum(capacity_x / reference_x) across gates.
  unpolled-account -- an account with no fresh poll / no resolvable
                       capacity_x -> EXCLUDED from the pool set (never a
                       ratio-1 fallback), and release_suppressed=True.

For every scenario the test first asserts the scenario's KEY PROPERTY
directly against a *freshly computed* snapshot (so the property is provably
exercised, not just baked into a golden nobody re-checks), then compares the
full snapshot field-by-field against the frozen tests/fixtures/replay/<scenario>/golden.json
with a tight float tolerance. `test_replay_deterministic` separately proves
two replay_forecast() calls on the same fixture are byte-identical.
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import cus  # noqa: E402

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "replay"

FLOAT_ABS_TOL = 1e-9


def _load_golden(scenario: str) -> dict:
    return json.loads((FIXTURES_ROOT / scenario / "golden.json").read_text())


def _assert_matches_golden(actual, expected, path: str = "$") -> None:
    """Recursive field-by-field comparison with a tight float tolerance.

    `actual` is a freshly computed snapshot (may contain real float/Decimal/
    datetime-ish values); `expected` is the golden, loaded straight from JSON
    (so every leaf is already str/int/float/bool/None/list/dict). We coerce
    `actual`'s leaves the same way `json.dumps(..., default=str)` did when the
    golden was frozen, so the comparison is apples to apples.
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected dict, got {type(actual)}"
        assert sorted(actual.keys()) == sorted(expected.keys()), (
            f"{path}: key mismatch -- actual={sorted(actual.keys())} "
            f"expected={sorted(expected.keys())}")
        for key in expected:
            _assert_matches_golden(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected list, got {type(actual)}"
        assert len(actual) == len(expected), (
            f"{path}: length mismatch -- actual={len(actual)} expected={len(expected)}")
        for i, (a, e) in enumerate(zip(actual, expected)):
            _assert_matches_golden(a, e, f"{path}[{i}]")
    elif isinstance(expected, float):
        a = float(actual)
        if math.isnan(expected):
            assert math.isnan(a), f"{path}: expected NaN, got {a}"
        elif math.isinf(expected):
            assert a == expected, f"{path}: expected {expected}, got {a}"
        else:
            assert a == pytest.approx(expected, abs=FLOAT_ABS_TOL), (
                f"{path}: {a} != {expected} (abs tol {FLOAT_ABS_TOL})")
    else:
        # int / str / bool / None -- coerce actual through str() only when
        # the golden itself is a str but actual isn't (datetime/Decimal
        # fields serialized via json.dumps(..., default=str) when frozen).
        if isinstance(expected, str) and not isinstance(actual, str):
            assert str(actual) == expected, f"{path}: {actual!r} != {expected!r}"
        else:
            assert actual == expected, f"{path}: {actual!r} != {expected!r}"


# ===========================================================================
# heavy-rotation
# ===========================================================================

def test_replay_heavy_rotation_pool_contribution_across_all_accounts():
    fixture_dir = FIXTURES_ROOT / "heavy-rotation"
    snapshot = cus.replay_forecast(fixture_dir)

    accounts = [f"A{i}" for i in range(1, 7)]
    tokens = {"A1": 10, "A2": 20, "A3": 30, "A4": 40, "A5": 50, "A6": 60}
    ratio = 20000.0 / 5.0  # capacity_x / reference_x, shared by all 6 accounts

    # KEY PROPERTY: the single rotated session's burn is attributed per
    # (account, interval) and every account's slice recovers exactly the
    # tokens burned while sessions.log had that account active -- summed
    # across all 6 accounts it reconstructs the full total.
    recovered_total = 0.0
    for name in accounts:
        recovered = snapshot["accounts"][name]["5h"]["burn_pct_per_min"] * ratio / 100.0
        assert recovered == pytest.approx(tokens[name], abs=1e-6), (
            f"{name}: recovered pinned burn {recovered} != {tokens[name]}")
        recovered_total += recovered
    assert recovered_total == pytest.approx(sum(tokens.values()), abs=1e-6)

    # The one session in the table shows all 6 accounts sharing its burn,
    # proportional to how much each account actually carried.
    assert len(snapshot["sessions"]) == 1
    shares = snapshot["sessions"][0]["account_shares"]
    assert sorted(shares.keys()) == accounts
    assert sum(shares.values()) == pytest.approx(1.0, abs=1e-9)

    _assert_matches_golden(snapshot, _load_golden("heavy-rotation"))


# ===========================================================================
# reset-crossing
# ===========================================================================

def test_replay_reset_crossing_staggered_pool_eta():
    fixture_dir = FIXTURES_ROOT / "reset-crossing"
    snapshot = cus.replay_forecast(fixture_dir)

    true_eta = snapshot["pool"]["5h"]["exhaustion_eta_min"]
    assert true_eta is not None

    # KEY PROPERTY: the pool ETA is the first crossing over the STAGGERED
    # reset knots (R resets at t=5, L at t=8), not a naive
    # Sum(remaining)/Sum(burn) = (960+960)/(20+20) = 48.0 min estimate that
    # ignores the reset ramps entirely.
    naive_eta = (960.0 + 960.0) / (20.0 + 20.0)
    assert naive_eta == pytest.approx(48.0)
    assert true_eta > naive_eta + 10.0, (
        f"eta {true_eta} is not clearly later than the naive {naive_eta} -- "
        "the staggered reset ramp doesn't look like it's being applied")

    # Hand-derived exact piecewise-linear crossing (both accounts' post-reset
    # slopes are identical and negative, floors at t=83/t=85.625; see
    # task-29-report.md for the full derivation). The bracket-then-bisect
    # root-finder stops within TTE_TOL=0.5 min of the true root.
    assert true_eta == pytest.approx(85.625, abs=1.0)

    _assert_matches_golden(snapshot, _load_golden("reset-crossing"))


# ===========================================================================
# mixed-tier
# ===========================================================================

def test_replay_mixed_tier_pool_capacity_sums_ratios():
    fixture_dir = FIXTURES_ROOT / "mixed-tier"
    snapshot = cus.replay_forecast(fixture_dir)

    # KEY PROPERTY: pool capacity = Sum(capacity_x / reference_x) at each
    # window's gate -- a 20x (ratio 4.0) and a 5x (ratio 1.0) account pool to
    # capacity_units = gate/100 * (4.0 + 1.0), not an unweighted count.
    gate_5h = 94.0
    gate_7d = 80.0
    expected_5h = (gate_5h / 100.0) * (4.0 + 1.0)
    expected_7d = (gate_7d / 100.0) * (4.0 + 1.0)

    assert snapshot["pool"]["5h"]["capacity_units"] == pytest.approx(
        expected_5h, abs=FLOAT_ABS_TOL)
    assert snapshot["pool"]["7d"]["capacity_units"] == pytest.approx(
        expected_7d, abs=FLOAT_ABS_TOL)
    assert snapshot["accounts"]["Big"]["5h"]["gate"] == pytest.approx(gate_5h)
    assert snapshot["accounts"]["Small"]["5h"]["gate"] == pytest.approx(gate_5h)

    _assert_matches_golden(snapshot, _load_golden("mixed-tier"))


# ===========================================================================
# unpolled-account
# ===========================================================================

def test_replay_unpolled_account_excluded_and_release_suppressed():
    fixture_dir = FIXTURES_ROOT / "unpolled-account"
    snapshot = cus.replay_forecast(fixture_dir)

    # KEY PROPERTY: Ghost (no capacity_x, no last_poll_ts) is excluded from
    # the pool set entirely -- the pool capacity reflects Known alone, never
    # a ratio-1 fallback for the unresolvable account.
    gate_5h = 94.0
    expected_known_only = (gate_5h / 100.0) * 4.0
    assert snapshot["pool"]["5h"]["capacity_units"] == pytest.approx(
        expected_known_only, abs=FLOAT_ABS_TOL)

    # And because a configured account couldn't be resolved/polled, both
    # windows report the pool figures as suppressed rather than silently
    # optimistic.
    assert snapshot["pool"]["5h"]["release_suppressed"] is True
    assert snapshot["pool"]["7d"]["release_suppressed"] is True

    _assert_matches_golden(snapshot, _load_golden("unpolled-account"))


# ===========================================================================
# determinism
# ===========================================================================

@pytest.mark.parametrize(
    "scenario",
    ["heavy-rotation", "reset-crossing", "mixed-tier", "unpolled-account"],
)
def test_replay_deterministic(scenario):
    fixture_dir = FIXTURES_ROOT / scenario

    first = cus.replay_forecast(fixture_dir)
    second = cus.replay_forecast(fixture_dir)

    first_json = json.dumps(first, indent=2, sort_keys=True, default=str)
    second_json = json.dumps(second, indent=2, sort_keys=True, default=str)
    assert first_json == second_json, (
        f"{scenario}: two replay_forecast() runs on the same fixture diverged "
        "-- check for wall-clock or dict-ordering leaks")
