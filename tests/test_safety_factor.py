"""Task 17 (spec-2 token-pressure forecaster, STAGE 1): safety-factor
widening curve -- ``_safety_factor`` (G4), the load-bearing "never blow the
limit" defense (§3; gains shadow-tuned).

``_safety_factor(residual_fraction, condition_number, cfg) -> float`` grows
monotonically with Task 16's ``fit_burn_weights`` output quality signals
(``residual_fraction``, ``condition_number``) so a low-confidence fit --
``"seed-fallback"``/``"insufficient-data"``, which carry high/inf condition
and residual -- makes the forecaster throttle MORE conservatively (a wider
safety factor). Floored at ``safety_factor_base`` (1.2), capped at
``safety_factor_max`` (3.0); both bounds hold even for an ``inf`` input
(Task 16's ``_condition_number`` returns ``math.inf`` for a singular Gram,
and ``_pressure_residual_fraction`` can return ``inf`` for an all-zero
``b`` with nonzero residual) -- the return is always a finite float in
``[base, max]``.

HARNESS (reused by every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Add the repo root to ``sys.path`` and ``import cus``. Run with ``python -m
pytest tests/ -q`` (same command CI uses); a single file runs standalone via
``python tests/test_safety_factor.py``.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import cus  # noqa: E402


# Empty cfg everywhere below -> every default applies:
#   safety_factor_base=1.2, safety_factor_max=3.0,
#   safety_factor_residual_gain=1.8, safety_factor_cond_gain=0.6,
#   cond_max=1.0e6 (weight_refit.cond_max default).
EMPTY_CFG: dict = {}


def test_floor_at_base():
    # residual=0, cond=1 -> log10(1)=0 and residual term 0, so raw == base
    # exactly, and the clamp is a no-op.
    result = cus._safety_factor(0.0, 1.0, EMPTY_CFG)
    assert result == pytest.approx(1.2)


def test_cap_at_max():
    # Huge residual (well past what any gain could keep under the cap) plus
    # an inf condition number (Task 16's singular-Gram fallback) must clamp
    # to exactly safety_factor_max -- and, critically, be a FINITE float,
    # never inf/nan leaking out of the clamp.
    result = cus._safety_factor(1.0e9, math.inf, EMPTY_CFG)
    assert result == pytest.approx(3.0)
    assert math.isfinite(result)


def test_monotone_in_residual():
    cond = 100.0
    residuals = [0.0, 0.01, 0.1, 0.5, 1.0, 10.0, 1.0e6, math.inf]
    values = [cus._safety_factor(r, cond, EMPTY_CFG) for r in residuals]
    for a, b in zip(values, values[1:]):
        assert b >= a - 1e-12


def test_monotone_in_condition():
    residual = 0.05
    conditions = [1.0, 10.0, 1.0e2, 1.0e3, 1.0e4, 1.0e5, 1.0e6, 1.0e9, math.inf]
    values = [cus._safety_factor(residual, c, EMPTY_CFG) for c in conditions]
    for a, b in zip(values, values[1:]):
        assert b >= a - 1e-12


def test_seed_fallback_widens():
    # A seed-fallback/insufficient-data-style input: high residual + inf
    # condition (exactly what Task 16 publishes on those two source paths)
    # must widen strictly above the floor, toward the cap.
    result = cus._safety_factor(0.5, math.inf, EMPTY_CFG)
    assert result > 1.2
    assert result <= 3.0


def test_nan_inputs_are_conservative_and_finite():
    # A nan quality signal means "fit confidence is unknown/degenerate" --
    # this must NEVER leak nan out of a load-bearing "never blow the limit"
    # function (Python's min/max, and thus _clamp, keep the first argument
    # on a nan comparison, so an uncoerced nan would silently propagate to
    # the return and make every downstream comparison False -> a dangerous
    # silent UNDER-throttle). Instead nan must widen toward the CAP, the
    # conservative extreme, never collapse to the floor.

    # nan residual_fraction -> coerced to +inf -> saturates the residual
    # term and clamps to exactly safety_factor_max, same as an explicit
    # inf residual (test_cap_at_max) and strictly wider than a comparable
    # finite-residual case.
    nan_residual = cus._safety_factor(math.nan, 1.0, EMPTY_CFG)
    finite_residual = cus._safety_factor(0.5, 1.0, EMPTY_CFG)
    assert math.isfinite(nan_residual)
    assert nan_residual == pytest.approx(3.0)
    assert nan_residual >= finite_residual

    # nan condition_number -> coerced to +inf -> the cond term saturates at
    # cond_gain, matching the explicit condition_number=inf result exactly:
    # base + cond_gain*1 = 1.2 + 0.6 = 1.8.
    nan_condition = cus._safety_factor(0.0, math.nan, EMPTY_CFG)
    inf_condition = cus._safety_factor(0.0, math.inf, EMPTY_CFG)
    assert math.isfinite(nan_condition)
    assert nan_condition == pytest.approx(1.8)
    assert nan_condition == pytest.approx(inf_condition)

    for result in (nan_residual, nan_condition):
        assert not math.isnan(result)
        assert not math.isinf(result)
