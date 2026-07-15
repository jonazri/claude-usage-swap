"""Task 2 (spec-2 token-pressure forecaster, STAGE 1): gate-wrapped reference
units + burn-units + pool eligibility (§10.10, C1).

Convert each account's %/burn to reference units measured **to the effective
gate** (§10.10, NOT to 100), and decide which accounts may enter the rotatable
pool (C1: active AND capacity_x populated AND pct < gate; a ratio-1 neutral
fallback account is never eligible).

Pinned gates (G3): ``gate_5h = config['thresholds']['steps'][-1]`` read LIVE
(currently 94, never a hardcoded literal — validate non-empty, else cus's own
default ``[50,75,90]`` top = 90); ``gate_7d = hard_7d_cap_pct = 80``.

HARNESS: cus.py imports as a plain module; add the repo root to ``sys.path``.
Run ``python -m pytest tests/ -q``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# reference_x pinned to 5 (the live production pin, FACT #4); a 20x and a 5x
# account so ratios are 4.0 and 1.0.
PINNED_CFG = {
    "capacity_aware": {"enabled": True, "reference_x": 5},
    "accounts": [
        {"name": "big", "capacity_x": 20},
        {"name": "ref", "capacity_x": 5},
    ],
    "thresholds": {"steps": [70, 85, 94]},
}


# ---- gate derivation (read live from thresholds.steps, never a pinned 94) ----

def test_gate_5h_from_thresholds_steps():
    """gate_5h is the LIVE top ladder step, not a hardcoded 94."""
    cfg = {"thresholds": {"steps": [70, 85, 90]}}
    assert cus._pressure_gate("5h", cfg) == 90.0  # not 94


def test_gate_5h_absent_or_empty_falls_to_cus_default_top():
    """Absent / empty steps -> cus's own in-code default [50,75,90] top = 90,
    never a pinned 94 (G3: a defaulted ladder must not mis-forecast)."""
    assert cus._pressure_gate("5h", {}) == 90.0
    assert cus._pressure_gate("5h", {"thresholds": {"steps": []}}) == 90.0
    assert cus._pressure_gate("5h", {"thresholds": {}}) == 90.0


def test_gate_7d_is_hard_cap_80():
    """gate_7d = hard_7d_cap_pct (default 80), NOT the level-bound weekly 95."""
    assert cus._pressure_gate("7d", {}) == 80.0


# ---- ratio (capacity_x / reference_x from the PURE helpers) ----

def test_ratio_20x_5x():
    """ratio = capacity_x/reference_x built from _account_raw_capacity_x +
    the side-effect-free _pressure_resolve_reference_x (20x -> 4.0, 5x -> 1.0)."""
    state = {"accounts": {"big": {}, "ref": {}}}
    assert cus._pressure_ratio("big", state, PINNED_CFG) == 4.0
    assert cus._pressure_ratio("ref", state, PINNED_CFG) == 1.0


def test_ratio_unknown_tier_is_neutral_one():
    """An account with no known tier resolves to the neutral ratio 1.0
    (matches _account_capacity_x's neutral fallback), never a divide error."""
    cfg = {"capacity_aware": {"reference_x": 5}, "accounts": [{"name": "mystery"}]}
    assert cus._pressure_ratio("mystery", {"accounts": {}}, cfg) == 1.0


# ---- gate-wrapped remaining units (measured TO the gate, not to 100) ----

def test_remaining_to_gate_not_100():
    """20x at 90% for 5h -> ((94-90)/100)*4.0 = 0.16 (to the gate, §10.10),
    NOT ((100-90)/100)*4.0 = 0.40."""
    assert cus._pressure_remaining_units(90.0, 94.0, 4.0) == 0.16


def test_above_gate_clamps_zero():
    """pct above the gate clamps remaining units to 0.0 (never negative)."""
    assert cus._pressure_remaining_units(96.0, 94.0, 4.0) == 0.0


def test_cap_units_fresh_window():
    """C_w = (gate/100)*ratio (fresh-window capacity)."""
    assert cus._pressure_cap_units(94.0, 4.0) == 94.0 / 100.0 * 4.0


def test_burn_units():
    """0.5 %/min on a 20x account -> (0.5/100)*4.0 = 0.02 units/min."""
    assert cus._pressure_burn_units(0.5, 4.0) == 0.02


# ---- pool eligibility (C1: active AND capacity_x populated AND pct < gate) ----

def test_pool_excludes_gated_and_unpolled():
    """Include iff active AND capacity_x POPULATED (never ratio-1 neutral
    fallback — C1) AND pct < gate. A gated account, a ratio-1 (unknown-tier)
    account, and a disabled account are all dropped; a genuinely-known
    below-gate account is kept."""
    cfg = {
        "capacity_aware": {"enabled": True, "reference_x": 5},
        "thresholds": {"steps": [70, 85, 94]},
        "accounts": [
            {"name": "healthy", "capacity_x": 20},   # known + below gate -> IN
            {"name": "gated", "capacity_x": 20},      # known but pct >= gate -> OUT
            {"name": "unknown"},                       # no tier -> ratio-1 -> OUT
            {"name": "off", "capacity_x": 5, "disabled": True},  # disabled -> OUT
        ],
    }
    state = {"accounts": {
        "healthy": {"current_5h_pct": 50.0},
        "gated": {"current_5h_pct": 96.0},
        "unknown": {"current_5h_pct": 10.0},
        "off": {"current_5h_pct": 10.0},
    }}
    assert cus._pressure_pool_set(state, "5h", cfg) == ["healthy"]


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
