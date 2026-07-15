"""Gate-off golden fixtures for the capacity-aware anti-herding rollout
(Phase 0b, spec Rollout §1: docs/plans/2026-07-10-capacity-aware-anti-herding.md).

Purpose: freeze CURRENT (unmodified cus.py) picker/decision behavior across a
representative scenario set — tests/capacity_fixtures.py — into
tests/fixtures/capacity_aware/goldens.json. Later tasks add an actual
`capacity_aware` gate; this test proves gate-off reproduces today's behavior
BIT-FOR-BIT, because right now there is no such key at all and the code paths
are identical regardless of it. Two config variants are run per scenario (task
brief item 4):
  - no `capacity_aware` key in config at all (today's real-world config shape)
  - `{"capacity_aware": {"enabled": False}}` merged in (the shape a later task
    will introduce)
Both must match the SAME golden record, since no code reads that key yet.

Regeneration: `CAPACITY_GOLDENS_REGEN=1 python3 -m pytest
tests/test_capacity_gate_off_goldens.py` recomputes and overwrites
goldens.json. Default mode only COMPARES and fails on any drift — once
committed, goldens.json must never change again (it is the bit-for-bit
gate-off contract for later tasks).

Only stable strings are recorded (pick_swap_target's chosen name, and
decide_swap's action/gate/target) — never scores, floats, timestamps, or
reason text, all of which are allowed to legitimately vary run to run even
when the DECISION doesn't.

Run standalone: python3 -m pytest tests/test_capacity_gate_off_goldens.py
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cus  # noqa: E402
from capacity_fixtures import build_scenarios, usage_from_state  # noqa: E402

GOLDENS_PATH = Path(__file__).resolve().parent / "fixtures" / "capacity_aware" / "goldens.json"

# The two gate-off config shapes every scenario must reproduce identically
# (task brief item 4): today's real shape (no key at all) and the future
# gate's explicit off-switch.
GATE_OFF_VARIANTS = {
    "no_key": {},
    "disabled": {"capacity_aware": {"enabled": False}},
}


def _record_for(state: dict, config: dict) -> dict:
    """Run ONE scenario's (state, config) through pick_swap_target +
    decide_swap and return only stable strings — never scores/floats/
    timestamps/reason text (those legitimately vary and would make the
    golden flaky for no behavioral reason)."""
    pick_state = copy.deepcopy(state)
    pick_config = copy.deepcopy(config)
    target = cus.pick_swap_target(pick_state, pick_config)

    decide_state = copy.deepcopy(state)
    decide_config = copy.deepcopy(config)
    usage = usage_from_state(decide_state)
    trace: dict = {}
    decision = cus.decide_swap(decide_state, decide_config, usage, trace)

    return {
        "pick": target.name if target is not None else None,
        "action": trace.get("action"),
        "gate": trace.get("gate"),
        "target": decision.target if decision is not None else None,
    }


def _compute_all_goldens() -> dict[str, dict]:
    """Compute the golden record for every scenario using the "no capacity_aware
    key at all" variant — the real-world config shape today. The comparison
    test separately re-verifies the "explicitly disabled" variant matches too."""
    goldens: dict[str, dict] = {}
    for name, state, config in build_scenarios():
        merged = cus.deep_merge(config, GATE_OFF_VARIANTS["no_key"])
        goldens[name] = _record_for(state, merged)
    return goldens


def test_regenerate_goldens_if_requested():
    """Not a real assertion — when CAPACITY_GOLDENS_REGEN=1, (re)writes
    goldens.json from the current scenario set and skips comparison. Under
    normal `pytest tests/` runs this env var is unset, so this test is a
    single cheap no-op assertion."""
    if not os.environ.get("CAPACITY_GOLDENS_REGEN"):
        assert True
        return
    goldens = _compute_all_goldens()
    GOLDENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDENS_PATH.write_text(json.dumps(goldens, indent=2, sort_keys=True) + "\n")


def _load_goldens() -> dict[str, dict]:
    assert GOLDENS_PATH.exists(), (
        f"{GOLDENS_PATH} is missing. Generate it once with:\n"
        f"  CAPACITY_GOLDENS_REGEN=1 python3 -m pytest tests/test_capacity_gate_off_goldens.py\n"
        f"then commit tests/fixtures/capacity_aware/goldens.json."
    )
    return json.loads(GOLDENS_PATH.read_text())


def test_gate_off_matches_goldens_no_capacity_aware_key():
    """Today's real config shape: no `capacity_aware` key present at all."""
    goldens = _load_goldens()
    for name, state, config in build_scenarios():
        merged = cus.deep_merge(config, GATE_OFF_VARIANTS["no_key"])
        got = _record_for(state, merged)
        assert name in goldens, f"scenario {name!r} has no golden recorded — regenerate goldens.json"
        assert got == goldens[name], f"scenario {name!r} (no capacity_aware key) drifted: {got} != {goldens[name]}"


def test_gate_off_matches_goldens_capacity_aware_disabled():
    """Future gate's explicit off-switch: {"capacity_aware": {"enabled": False}}.
    Must byte-for-byte match the SAME golden as the no-key variant above,
    because no code reads this key yet — identical code paths either way."""
    goldens = _load_goldens()
    for name, state, config in build_scenarios():
        merged = cus.deep_merge(config, GATE_OFF_VARIANTS["disabled"])
        got = _record_for(state, merged)
        assert name in goldens, f"scenario {name!r} has no golden recorded — regenerate goldens.json"
        assert got == goldens[name], f"scenario {name!r} (capacity_aware.enabled=False) drifted: {got} != {goldens[name]}"


def test_both_gate_off_variants_agree_with_each_other():
    """Belt-and-suspenders: the two variants must match EACH OTHER too, not
    just both happen to match the golden independently."""
    for name, state, config in build_scenarios():
        no_key = _record_for(state, cus.deep_merge(config, GATE_OFF_VARIANTS["no_key"]))
        disabled = _record_for(state, cus.deep_merge(config, GATE_OFF_VARIANTS["disabled"]))
        assert no_key == disabled, f"scenario {name!r}: no-key vs disabled variants disagree: {no_key} != {disabled}"


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
