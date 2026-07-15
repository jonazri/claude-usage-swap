"""Task 19 (spec-2 token-pressure forecaster, STAGE 1, Phase D closer):
blindness / cold-start guard (§3 "Blindness guard", §1 rule 1).

Right after a daemon start/restart, Task 10's tail reader (`_read_active_tails`)
hasn't caught up yet -- per-session burn attribution is temporarily
incomplete, so the residual is high for a BENIGN reason (catching up), not a
real attribution failure. Under this "blindness" the forecaster must HOLD
the attribution-dependent acting paths (§5.2 targeting AND the residual-
driven safety-factor widening) while STILL letting the SUPPLY-derived
breach forecast (state.json burn rates alone, needs no per-session
attribution) drive §5.4 escalate-before-gate.

Covers `_attribution_confidence` -- the one pure function Task 19
introduces.

HARNESS (reused by every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Add the repo root to ``sys.path`` and ``import cus``. Run with ``python -m
pytest tests/ -q`` (same command CI uses); a single file runs standalone via
``python tests/test_pressure_blindness.py``.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


def test_coldstart_detected_as_blindness():
    """Right after a daemon (re)start, `offsets` is empty -- no active
    session's transcript has been read yet. Every active session is
    unread, so the high residual this cycle is cold-start backlog, not a
    real attribution failure: blindness=True."""
    offsets: dict[Path, int] = {}
    active_sessions = ["s1", "s2", "s3", "s4"]

    result = cus._attribution_confidence(offsets, active_sessions, NOW)

    assert result["blindness"] is True
    assert result["confidence"] == pytest.approx(0.0)
    assert "cold-start" in result["reason"]


def test_steady_state_residual_not_blindness():
    """The reader has caught up: every active session has an `offsets`
    entry (matched by the established stem == session_id transcript-naming
    convention). A nonzero steady-state residual is a real fit-quality
    signal, never blindness -- blindness=False regardless of how much
    residual remains (this function doesn't even see residual_fraction;
    that's Task 11's `_attribute_burn` output, merged in by the caller)."""
    offsets = {
        Path("/home/yaz/.claude/projects/proj/s1.jsonl"): 4096,
        Path("/home/yaz/.claude/projects/proj/s2.jsonl"): 8192,
    }
    active_sessions = ["s1", "s2"]

    result = cus._attribution_confidence(offsets, active_sessions, NOW)

    assert result["blindness"] is False
    assert result["confidence"] == pytest.approx(1.0)
    assert "steady-state" in result["reason"]


def test_blindness_holds_targeting_and_widening():
    """Under blindness, both attribution-dependent acting paths -- §5.2
    targeting and the residual-driven safety-factor widening -- must be
    held (suppressed)."""
    offsets: dict[Path, int] = {}
    active_sessions = ["s1", "s2"]

    result = cus._attribution_confidence(offsets, active_sessions, NOW)

    assert result["blindness"] is True
    assert result["suppress_targeting"] is True
    assert result["suppress_residual_widening"] is True


def test_supply_escalate_still_fires():
    """Under blindness, the SUPPLY-derived §5.4 escalate-before-gate must
    NOT be suppressed -- it is computed from state.json burn rates alone
    and needs no per-session attribution at all."""
    offsets: dict[Path, int] = {}
    active_sessions = ["s1", "s2", "s3"]

    result = cus._attribution_confidence(offsets, active_sessions, NOW)

    assert result["blindness"] is True  # sanity: this test is exercising the blind path
    assert result["allow_supply_escalation"] is True

    # Non-blind path too -- supply escalation is ALWAYS on, blindness or not.
    caught_up = {
        Path("/proj/s1.jsonl"): 1,
        Path("/proj/s2.jsonl"): 1,
        Path("/proj/s3.jsonl"): 1,
    }
    result_ok = cus._attribution_confidence(caught_up, active_sessions, NOW)
    assert result_ok["blindness"] is False
    assert result_ok["allow_supply_escalation"] is True


def test_confidence_published():
    """`confidence`/`blindness`/`reason` are present and well-formed, and
    compose cleanly into the pressure.json `attribution` block Task 20
    assembles ({confidence, blindness, residual_fraction, reason}) once the
    caller merges in Task 11's `residual_fraction`."""
    offsets = {Path("/proj/s1.jsonl"): 10}
    active_sessions = ["s1", "s2"]  # s2 unread -> partial

    result = cus._attribution_confidence(offsets, active_sessions, NOW)

    assert isinstance(result["confidence"], float)
    assert 0.0 <= result["confidence"] <= 1.0
    assert isinstance(result["blindness"], bool)
    assert isinstance(result["reason"], str) and result["reason"]

    # Composes cleanly into the published attribution block (Task 20 merges
    # in residual_fraction from Task 11's AttributionTable; simulated here).
    published = {
        "confidence": result["confidence"],
        "blindness": result["blindness"],
        "residual_fraction": 0.42,
        "reason": result["reason"],
    }
    assert set(published) == {"confidence", "blindness", "residual_fraction", "reason"}


def test_no_active_sessions_is_not_blind():
    """No active sessions at all -> nothing to be blind about: confidence
    1.0, blindness False (not an edge case that should ever suppress
    targeting/widening when there's simply nothing to target)."""
    result = cus._attribution_confidence({}, [], NOW)

    assert result["blindness"] is False
    assert result["confidence"] == pytest.approx(1.0)
    assert result["suppress_targeting"] is False
    assert result["suppress_residual_widening"] is False
    assert result["allow_supply_escalation"] is True


def test_partial_unread_below_threshold_not_blind():
    """A small trickle of freshly-launched sessions (not yet through the
    reader's next cycle) mid-steady-state must NOT blind the whole fleet --
    only a MEANINGFUL fraction unread counts as cold-start. 1 of 4 unread
    (25%) stays below the guard's threshold: blindness=False."""
    offsets = {
        Path("/proj/s1.jsonl"): 10,
        Path("/proj/s2.jsonl"): 10,
        Path("/proj/s3.jsonl"): 10,
    }
    active_sessions = ["s1", "s2", "s3", "s4"]  # s4 freshly launched, unread

    result = cus._attribution_confidence(offsets, active_sessions, NOW)

    assert result["blindness"] is False
    assert result["confidence"] == pytest.approx(0.75)


def test_accepts_path_representation_for_active_sessions():
    """A caller holding transcript Paths (rather than bare session_id
    strings) on hand gets a correct match too -- direct membership in
    `offsets`, no stem-matching needed."""
    p1 = Path("/proj/s1.jsonl")
    p2 = Path("/proj/s2.jsonl")
    offsets = {p1: 10}
    active_sessions = [p1, p2]  # p2 unread

    result = cus._attribution_confidence(offsets, active_sessions, NOW)

    assert result["confidence"] == pytest.approx(0.5)


if __name__ == "__main__":
    import pytest as _pytest

    sys.exit(_pytest.main([__file__, "-q"]))
