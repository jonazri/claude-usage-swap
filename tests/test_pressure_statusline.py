"""Task 22 (spec-2 token-pressure forecaster, STAGE 1): `cus statusline`
pressure glyph — a compact one-line, non-`ok`-only glyph appended to the
existing `cus statusline` output so the box's status line shows an
at-a-glance token-pressure warning.

`_pressure_statusline_glyph(snapshot)` is a PURE formatter over an
already-computed pressure.json `snapshot` (Task 20's `_pressure_snapshot`
schema, see `tests/test_pressure_json.py`) — it reads
`snapshot["level"]`/`snapshot["binding"]` plus the corresponding
`pool`/`accounts` window block for the binding's required reduction, and
NEVER recomputes, mutates, or writes anything (G0, the same read-only
contract every `tests/test_pressure_*.py` pins).

HARNESS (reused by every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Add the repo root to ``sys.path`` and ``import cus``. Run with ``python -m
pytest tests/ -q`` (same command CI uses); a single file runs standalone via
``python tests/test_pressure_statusline.py``.
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def test_ok_empty():
    """level == "ok" -> no glyph, regardless of any (even absent) binding."""
    snapshot = {"level": "ok", "binding": None}
    assert cus._pressure_statusline_glyph(snapshot) == ""


def test_pool_glyph():
    """Pool binding: eta_min=144 -> 2.4h (144/60). pool.5h.
    required_reduction_units_per_min=0.18 reference units/min -> a
    reference-account-equivalent -18% (the same units=pct/100*ratio
    convention `_required_reduction_pinned` @5686 uses for a ratio=1
    account: units * 100 = pct)."""
    snapshot = {
        "level": "breach",
        "binding": {
            "view": "pool",
            "name": "pool:5h",
            "constraint": "5h",
            "window": "5h",
            "eta_min": 144,
        },
        "pool": {
            "5h": {
                "capacity_units": 1.0,
                "remaining_units": 0.05,
                "burn_units_per_min": 0.01,
                "exhaustion_eta_min": 144,
                "required_reduction_units_per_min": 0.18,
            },
        },
        "accounts": {},
    }
    assert cus._pressure_statusline_glyph(snapshot) == "⚡pool 2.4h -18%"


def test_account_glyph():
    """Account binding: eta_min=90 -> 1.5h. accounts.myjli.5h.
    required_reduction_pct_per_min is already a percent -- used as-is (no
    units->pct rescale, unlike the pool form)."""
    snapshot = {
        "level": "gate",
        "binding": {
            "view": "account",
            "name": "myjli",
            "constraint": "5h",
            "window": "5h",
            "eta_min": 90,
        },
        "pool": {},
        "accounts": {
            "myjli": {
                "5h": {
                    "pct": 88.0,
                    "gate": 94.0,
                    "remaining_units": 0.02,
                    "burn_pct_per_min": 0.4,
                    "pinned_eta_min": 90,
                    "required_reduction_pct_per_min": 25.0,
                },
            },
        },
    }
    assert cus._pressure_statusline_glyph(snapshot) == "⚡myjli 1.5h -25%"


def test_fable_weekly_glyph():
    """Fable-weekly (level-bound, G6) binding: `constraint == "fable_weekly"`,
    `window == "7d"` (a literal that always exists as a key, per
    `_pressure_binding_view`/Task 9 §5829-5836), `eta_min == 0.0`. This is
    NOT a real 7d breach ETA -- Fable has no numeric eta/required-reduction
    of its own (a qualitative Fable-cap ask). The account's ORDINARY 7d
    `required_reduction_pct_per_min` is 0.0 here (the exact shape that used
    to produce the misleading `⚡myjli 0.0h -0%`, which reads as "nothing to
    do" for a CRITICAL condition). The fix must render a qualitative glyph
    instead -- no fake eta/percent, and never `""` (must not hide a critical
    condition)."""
    snapshot = {
        "level": "critical",
        "binding": {
            "view": "account",
            "name": "myjli",
            "constraint": "fable_weekly",
            "window": "7d",
            "eta_min": 0.0,
        },
        "pool": {},
        "accounts": {
            "myjli": {
                "7d": {
                    "pct": 10.0,
                    "gate": 94.0,
                    "remaining_units": 0.9,
                    "burn_pct_per_min": 0.0,
                    "pinned_eta_min": None,
                    "required_reduction_pct_per_min": 0.0,
                },
            },
        },
    }
    result = cus._pressure_statusline_glyph(snapshot)
    assert result == "⚡myjli fable"
    assert result != "⚡myjli 0.0h -0%"
    assert result != ""


def test_no_side_effects(monkeypatch):
    """Calling the glyph never writes pressure.json, never recomputes the
    snapshot (via `_pressure_snapshot`/`_pressure_write_json`), and never
    mutates the snapshot dict it was handed (G0)."""

    def _boom(*_a, **_k):
        raise AssertionError("glyph formatter reached a mutating/recompute path")

    monkeypatch.setattr(cus, "_pressure_write_json", _boom)
    monkeypatch.setattr(cus, "_pressure_snapshot", _boom)

    snapshot = {
        "level": "breach",
        "binding": {
            "view": "pool",
            "name": "pool:5h",
            "constraint": "5h",
            "window": "5h",
            "eta_min": 144,
        },
        "pool": {
            "5h": {"required_reduction_units_per_min": 0.18},
        },
        "accounts": {},
    }
    before = copy.deepcopy(snapshot)
    result = cus._pressure_statusline_glyph(snapshot)
    assert result == "⚡pool 2.4h -18%"
    assert snapshot == before  # unmutated


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
