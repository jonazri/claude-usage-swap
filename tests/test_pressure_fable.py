"""Task 18 (spec-2 token-pressure forecaster, STAGE 1, Phase D -- LAST task):
Fable-weekly rate from % snapshots + level-bound binding (G8/FACT #7).

The Fable per-model weekly cap exposes ONLY a % level via
``accounts[name].per_model_weekly_pct["Fable"]`` (the literal string key) --
no per-model burn rate and no per-model reset timestamp, unlike the 5h/7d
windows. So `_fable_rate` DERIVES a %/min rate from consecutive % snapshots
(reusing `_compute_burn_rate`'s upward-only, drop-clamps-to-zero semantics),
and `_fable_binding` binds by raw LEVEL rather than a computed ETA -- a known
G8 limitation: the derived rate is shadow-only (computed, never wired into an
ETA that drives the binding) in v1.

HARNESS (reused by every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Add the repo root to ``sys.path`` and ``import cus``. Run with ``python -m
pytest tests/ -q`` (same command CI uses); a single file runs standalone via
``python tests/test_pressure_fable.py``.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

CFG = {
    "per_model_weekly": {"cap_pct": 95, "target_cap_pct": 90, "models": ["Fable"]},
    "pressure": {"weekly_gate_margin_pct": 2},
}


def _iso(dt):
    return dt.isoformat()


# ============================ _fable_rate ==================================
# %/min derived from consecutive (ts, pct) Fable snapshots, reusing
# _compute_burn_rate's upward-only semantics.


def test_fable_rate_from_snapshots():
    """A normal rising pair derives the same %/min `_compute_burn_rate` would
    give directly; a pair where the % DROPS between polls (the weekly window
    rolled over) clamps the rate to 0.0 -- never negative."""
    t0 = NOW
    t1 = NOW + timedelta(minutes=10)

    # Rising: 40.0 -> 44.0 over 10 min == 0.4 %/min, matching _compute_burn_rate
    # directly.
    rising = [(_iso(t0), 40.0), (_iso(t1), 44.0)]
    expected = cus._compute_burn_rate(40.0, 44.0, _iso(t0), _iso(t1))
    assert cus._fable_rate(rising) == pytest.approx(expected)
    assert cus._fable_rate(rising) == pytest.approx(0.4)

    # Dropping: 90.0 -> 10.0 (weekly reset happened between polls) -> 0.0,
    # NOT a large negative "rate".
    dropping = [(_iso(t0), 90.0), (_iso(t1), 10.0)]
    rate = cus._fable_rate(dropping)
    assert rate == 0.0
    assert rate >= 0.0

    # Fewer than two snapshots -> nothing to derive a slope from -> 0.0.
    assert cus._fable_rate([]) == 0.0
    assert cus._fable_rate([(_iso(t0), 50.0)]) == 0.0

    # Only the LATEST consecutive pair is used, even with a longer history.
    older = (_iso(t0 - timedelta(minutes=10)), 0.0)  # would imply a huge rate
    latest_pair_only = [older, (_iso(t0), 40.0), (_iso(t1), 44.0)]
    assert cus._fable_rate(latest_pair_only) == pytest.approx(0.4)


# ============================ literal "Fable" key ===========================


def test_fable_key_literal():
    """The per-model weekly dict key is the LITERAL string "Fable" (matching
    the production read at cus.py:5819 -- ``acct_state.get(
    "per_model_weekly_pct", {}).get("Fable")``). Building the snapshot list
    with that exact access pattern picks up the readings; a differently-cased
    "fable" key is invisible to it (dict lookup is case-sensitive), proving
    the literal-key convention actually matters."""
    t0 = NOW
    t1 = NOW + timedelta(minutes=5)

    # Two "polls" of the same account, exactly as state.json would hold them.
    poll_0 = {"per_model_weekly_pct": {"Fable": 20.0}, "last_poll_ts": _iso(t0)}
    poll_1 = {"per_model_weekly_pct": {"Fable": 25.0}, "last_poll_ts": _iso(t1)}

    def _extract(acct_state):
        # Exact production access pattern (cus.py:5819).
        return acct_state.get("per_model_weekly_pct", {}).get("Fable")

    snapshots = [(_iso(t0), _extract(poll_0)), (_iso(t1), _extract(poll_1))]
    assert snapshots == [(_iso(t0), 20.0), (_iso(t1), 25.0)]
    assert cus._fable_rate(snapshots) == pytest.approx(1.0)  # 5%/5min

    # A lowercase "fable" key is NOT the same key -- the literal access
    # pattern returns None for it, not the reading.
    wrong_case = {"per_model_weekly_pct": {"fable": 99.0}}
    assert _extract(wrong_case) is None


# ============================ _fable_binding ================================
# LEVEL-bound (not ETA-bound): critical within weekly_gate_margin_pct of
# cap_pct; no numeric eta/required_reduction ever.


def test_level_bound_within_2pct():
    """cap_pct=95, margin=2 -> threshold 93. At/above -> critical,
    level-bound, with NO numeric required_reduction (a qualitative
    Fable->Sonnet ask only). Below -> not critical."""
    at_threshold = cus._fable_binding(93.0, CFG)
    assert at_threshold["critical"] is True
    assert at_threshold["level"] == "critical"
    assert at_threshold["level_bound"] is True
    assert at_threshold["required_reduction"] is None

    above = cus._fable_binding(97.5, CFG)
    assert above["critical"] is True
    assert above["required_reduction"] is None

    just_below = cus._fable_binding(92.9, CFG)
    assert just_below["critical"] is False
    assert just_below["level"] != "critical"
    # Still level-bound in SHAPE (the binding always carries the field) even
    # when not currently tripped -- it just isn't critical.
    assert just_below["level_bound"] is True
    assert just_below["required_reduction"] is None

    # No reading at all -> not critical, "ok".
    none_reading = cus._fable_binding(None, CFG)
    assert none_reading["critical"] is False
    assert none_reading["level"] == "ok"

    # Defaults (cap_pct=95, margin=2) apply with an empty config too.
    default_cfg_critical = cus._fable_binding(93.0, {})
    assert default_cfg_critical["critical"] is True
    default_cfg_elevated = cus._fable_binding(92.9, {})
    assert default_cfg_elevated["critical"] is False


# ============================ weekly reset proxy =============================


def test_weekly_reset_proxy(monkeypatch):
    """Fable has no reset ts of its own -- a caller threads the account's
    real 7d reset (`projected_seven_day_reset`, the best available proxy)
    through `_fable_binding`'s optional `reset` kwarg, and it is carried
    straight into the binding for display. No reset supplied -> None."""
    fixed_reset = _iso(NOW + timedelta(hours=48))
    monkeypatch.setattr(cus, "projected_seven_day_reset",
                        lambda acct, cfg, now=None: fixed_reset)

    acct = {"seven_day_last_reset_ts": _iso(NOW - timedelta(hours=24))}
    reset = cus.projected_seven_day_reset(acct, CFG, NOW)
    assert reset == fixed_reset

    binding = cus._fable_binding(96.0, CFG, reset=reset)
    assert binding["reset"] == fixed_reset

    # Without a caller-supplied reset, the field is simply None -- Fable
    # binding never computes its own reset (it has no reset ts to compute
    # one from).
    no_reset_binding = cus._fable_binding(96.0, CFG)
    assert no_reset_binding["reset"] is None


# ============================ rate is shadow-only ============================


def test_rate_shadow_only():
    """The derived Fable rate is computed (a real, non-trivial value) but is
    NEVER turned into an ETA that drives the binding in v1: `_fable_binding`
    has no rate parameter at all, so a genuinely fast-climbing rate cannot
    reach it -- the binding's `eta` stays None regardless."""
    t0 = NOW
    t1 = NOW + timedelta(minutes=10)
    fast_climb = [(_iso(t0), 50.0), (_iso(t1), 90.0)]  # steep: 4%/min

    rate = cus._fable_rate(fast_climb)
    assert rate == pytest.approx(4.0)
    assert rate > 0.0  # genuinely computed, not a stub

    # Whatever the level (critical or not), the binding never carries an eta
    # -- it has no way to see `rate` at all.
    critical_binding = cus._fable_binding(96.0, CFG)
    assert critical_binding["eta"] is None
    assert critical_binding["critical"] is True

    elevated_binding = cus._fable_binding(50.0, CFG)
    assert elevated_binding["eta"] is None
    assert elevated_binding["critical"] is False

    # Structural guarantee, not just an empirical one: _fable_binding's own
    # signature has no rate/eta-producing parameter for the shadow rate to
    # arrive through.
    import inspect
    params = inspect.signature(cus._fable_binding).parameters
    assert "rate" not in params
    assert "eta" not in params


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
