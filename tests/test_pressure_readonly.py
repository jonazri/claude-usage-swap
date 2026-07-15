"""Task 1 (spec-2 token-pressure forecaster, STAGE 1): read-only state load +
side-effect-free ``reference_x`` pre-resolve — the G0 / §10.11 landmine.

The whole cus forecaster loads ``state.json`` READ-ONLY and never
``save_state``s. In particular it must resolve ``reference_x`` WITHOUT
``_resolve_reference_x`` @4833's two production side effects on the unpinned
first-caller path:

  1. persisting a bootstrapped ``state["capacity_reference_snapshot"]`` (an
     in-place mutation the daemon would later flush to disk), and
  2. emitting the SOS-instruction warning that ``_capacity_warnings`` @4964
     folds into a real operator ``SOSCondition`` (SOS builder @12108).

These tests pin the G0 invariant the forecaster inherits everywhere: a
pressure read NEVER calls ``save_state``, NEVER mutates the loaded state in
place, and leaves the live ``state.json`` byte-identical.

HARNESS (reused by every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Add the repo root to ``sys.path`` and ``import cus``; point ``cus.STATE_JSON``
at a tmp file to control the on-disk state. Run with ``python -m pytest
tests/ -q`` (same command CI uses); a single file runs standalone via
``python tests/test_pressure_readonly.py``.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# reference_x pinned to 5 (the live production pin, FACT #4). Fleet has a 20x
# and a 5x account so the *unpinned* path can resolve an observed fleet-min of
# 5 without any credentials on disk (config capacity_x wins in
# _account_raw_capacity_x@4716).
PINNED_CFG = {
    "capacity_aware": {"enabled": True, "reference_x": 5},
    "accounts": [
        {"name": "big", "capacity_x": 20},
        {"name": "ref", "capacity_x": 5},
    ],
    "thresholds": {"steps": [70, 85, 94]},
}

# Same fleet, but NO reference_x pin -> resolution must fall through
# pin -> snapshot -> observed fleet-min.
UNPINNED_CFG = {
    "capacity_aware": {"enabled": True},
    "accounts": [
        {"name": "big", "capacity_x": 20},
        {"name": "ref", "capacity_x": 5},
    ],
    "thresholds": {"steps": [70, 85, 94]},
}


def _write_state(tmp_path, state):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return p


def test_never_saves_state(tmp_path, monkeypatch):
    """Load + a compute over the snapshot never reaches save_state (G0)."""
    sp = _write_state(tmp_path, {"accounts": {"big": {}, "ref": {}}})
    monkeypatch.setattr(cus, "STATE_JSON", sp)

    def _boom(*_a, **_k):
        raise AssertionError("save_state called on the read-only pressure path")

    monkeypatch.setattr(cus, "save_state", _boom)

    state = cus._pressure_load_state(PINNED_CFG)
    ref = cus._pressure_resolve_reference_x(state, PINNED_CFG)  # stubbed compute
    assert ref == 5.0


def test_reference_x_pinned_no_snapshot_write(tmp_path, monkeypatch):
    """Pinned reference_x resolves to 5 without bootstrapping a snapshot, and
    the on-disk state.json is byte-identical (and mtime unchanged) after."""
    sp = _write_state(tmp_path, {"accounts": {"big": {}, "ref": {}}})
    monkeypatch.setattr(cus, "STATE_JSON", sp)

    before_bytes = sp.read_bytes()
    before_mtime = sp.stat().st_mtime_ns

    state = cus._pressure_load_state(PINNED_CFG)
    assert "capacity_reference_snapshot" not in state

    ref = cus._pressure_resolve_reference_x(state, PINNED_CFG)
    assert ref == 5.0
    # in-memory snapshot copy: the pinned path must NOT freeze a snapshot
    assert "capacity_reference_snapshot" not in state
    # on-disk: proven untouched
    assert sp.read_bytes() == before_bytes
    assert sp.stat().st_mtime_ns == before_mtime


def test_unpinned_no_sos(tmp_path, monkeypatch):
    """Unpinned + no snapshot falls to the observed fleet-min WITHOUT the SOS
    warning path and WITHOUT persisting the bootstrapped snapshot."""
    sp = _write_state(tmp_path, {"accounts": {"big": {}, "ref": {}}})
    monkeypatch.setattr(cus, "STATE_JSON", sp)

    def _boom_warn(*_a, **_k):
        raise AssertionError("_capacity_warnings (SOS path) reached from forecaster")

    # If the forecaster ever routed through _resolve_reference_x's warn/persist
    # branch, or called _capacity_warnings, these would fire.
    monkeypatch.setattr(cus, "_capacity_warnings", _boom_warn)

    before_bytes = sp.read_bytes()

    state = cus._pressure_load_state(UNPINNED_CFG)
    ref = cus._pressure_resolve_reference_x(state, UNPINNED_CFG)
    assert ref == 5.0  # observed fleet-min = min(20, 5)
    assert "capacity_reference_snapshot" not in state  # never bootstrapped
    assert sp.read_bytes() == before_bytes  # state.json untouched


def test_deepcopy_isolation(tmp_path, monkeypatch):
    """Mutating the returned snapshot never leaks into a fresh load_state()."""
    sp = _write_state(
        tmp_path,
        {"accounts": {"big": {"capacity_x": 20}}, "swap_history": []},
    )
    monkeypatch.setattr(cus, "STATE_JSON", sp)

    state = cus._pressure_load_state(PINNED_CFG)
    # Mutate nested + top-level structures on the snapshot.
    state["accounts"]["big"]["capacity_x"] = 999
    state["capacity_reference_snapshot"] = 123
    state["swap_history"].append("mutated")

    fresh = cus.load_state()
    assert fresh["accounts"]["big"]["capacity_x"] == 20
    assert "capacity_reference_snapshot" not in fresh
    assert fresh["swap_history"] == []


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
