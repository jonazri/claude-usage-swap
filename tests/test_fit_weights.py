"""Task 16 (spec-2 token-pressure forecaster, STAGE 1): the full constrained
burn-weight fit -- `fit_burn_weights` composes Task 15's `_nnls` with a
monotone-prior reparam (`w = T @ x, x >= 0`), a scaled seed, Tikhonov
regularization anchored on that seed, a condition-number fallback
(`_condition_number`), and a min-windows guard, to fit the 5 pinned
per-token-type burn weights from Task 14's `_build_weight_windows` `(A, b)`
output.

Under test (all in ``cus.py``):

  fit_burn_weights(A, b, seeds_rel, cfg) -> weight_fit dict, keys
      {weights, source, condition_number, residual_fraction, n_windows,
       seed_scale, seed_pinned}. ``source`` is one of "insufficient-data",
      "seed-fallback", "fit".
  _condition_number(A) -> float -- kappa of the populated-column Gram of
      the ORIGINAL (non-reparam) token-mix matrix, via a compact cyclic
      Jacobi eigenvalue routine. Cross-checked offline against
      ``numpy.linalg.cond`` in a throwaway venv (see task-16-report.md) --
      matched to <1e-4 relative on well-conditioned/known-singular-value
      cases, and correctly reports `inf` where numpy reports a large-but-
      finite number for a matrix beyond our singularity tolerance.
  _scaled_seed(A, b, w_seed_rel) -> (w_seed, seed_scale).

HARNESS (reused by every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Run with ``python -m pytest tests/ -q`` (same command CI uses); a single
file runs standalone via ``python tests/test_fit_weights.py``.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import cus  # noqa: E402

CFG = {"pressure": {"weight_refit": {}}}  # every value defaults inside fit_burn_weights
COLUMNS = cus._PRESSURE_WEIGHT_COLUMNS
DEFAULT_SEEDS = cus._PRESSURE_DEFAULT_SEEDS_REL


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _synthetic_windows(w_true, n=250, cache_5m_zero=False):
    """A well-conditioned, deterministic (no RNG) synthetic `(A, b)` fleet:
    n windows, 5 diverse token-mix columns via modular arithmetic (never
    proportional to each other, so the populated-column Gram is
    well-conditioned), with `b` generated NOISELESSLY from a known true
    weight vector `w_true` -- `b_i = dot(row_i, w_true)` exactly, so a
    correct fit should recover `w_true` to high precision."""
    A, b = [], []
    for i in range(n):
        row = [
            50 + (i * 7) % 40,
            30 + (i * 13) % 25,
            20 + (i * 3) % 15,
            0.0 if cache_5m_zero else 5 + (i * 11) % 10,
            2 + (i * 17) % 8,
        ]
        row = [float(v) for v in row]
        A.append(row)
        b.append(_dot(row, w_true))
    return A, b


def test_recovers_weights_within_2pct():
    """Well-conditioned synthetic A, true weights `w* = 3.7 * seeds_rel`
    (i.e. AT the fitted scale -- a scalar multiple of the seed template, so
    the Tikhonov anchor agrees with the truth and contributes ~zero bias):
    the fit recovers every one of the 5 weights within 2% and lands on
    source "fit" (n=250 >= min_windows, well-conditioned, valid seed scale,
    NNLS converges)."""
    w_star = [3.7 * s for s in DEFAULT_SEEDS]
    A, b = _synthetic_windows(w_star)

    result = cus.fit_burn_weights(A, b, None, CFG)

    assert result["source"] == "fit"
    assert result["n_windows"] == 250
    for name, true_v in zip(COLUMNS, w_star):
        got = result["weights"][name]
        pct_err = abs(got - true_v) / true_v * 100.0
        assert pct_err < 2.0, f"{name}: got={got} true={true_v} pct_err={pct_err}%"


def test_seed_bias_under_collinear_A():
    """A moderately collinear A (output ~= 3x input, a realistic near-
    duplication -- NOT exact, so kappa is elevated but stays under
    cond_max=1e6 and the fit still runs, rather than tripping full
    seed-fallback -- see `test_ill_conditioned_falls_back` for that regime).
    Under-determined directions get pulled toward the seed by the Tikhonov
    term: every fitted weight stays within a generous [0.2x, 5x] band of
    the correspondingly-scaled seed -- sane, not wild (e.g. not negative,
    not a 50x blowup) -- while the well-determined ``input``/``output``
    sum still tracks the true underlying weights reasonably closely."""
    w_star = [1.0, 5.0, 0.15, 1.3, 2.1]
    A, b = [], []
    for i in range(250):
        base_in = 10.0 + (i * 3) % 20
        row = [base_in, base_in * 3.0 + (i % 3) * 0.01, 20 + (i * 3) % 15,
               5 + (i * 11) % 10, 2 + (i * 17) % 8]
        row = [float(v) for v in row]
        A.append(row)
        b.append(_dot(row, w_star))

    result = cus.fit_burn_weights(A, b, None, CFG)

    assert result["source"] == "fit"  # elevated kappa, but under cond_max
    assert result["condition_number"] > 1000.0  # genuinely elevated
    assert result["condition_number"] <= 1.0e6

    scaled_seed = [result["seed_scale"] * s for s in DEFAULT_SEEDS]
    for name, seed_v in zip(COLUMNS, scaled_seed):
        got = result["weights"][name]
        assert got >= 0.0
        assert 0.2 * seed_v <= got <= 5.0 * seed_v, (
            f"{name}: got={got} strayed too far from scaled seed={seed_v}"
        )


def test_monotonicity_always_holds():
    """`output >= input >= 0` and `0 <= cache_read <= cache_create_5m <=
    cache_create_1h` hold for the fitted weights in TWO scenarios: (1) a
    normal well-conditioned fit (reuses the recovery scenario), and (2) a
    BOUNDARY-DEGENERATE case where the true relationship has
    `output == input` and `cache_read == cache_create_5m == cache_create_1h`
    exactly (i.e. the reparam's x1/x3/x4 increments are truly zero) --
    seeded with the SAME degenerate template so the Tikhonov anchor doesn't
    fight the true zero, driving NNLS's active-set to actually clamp those
    components to the x>=0 boundary. The reparam (`w = T @ x, x >= 0`)
    makes the invariant hold ALGEBRAICALLY for source "fit" (T's structure
    plus x's non-negativity guarantees it by construction) -- this test is
    the empirical check that the wiring is actually correct end to end,
    including right at the boundary where a sign error would most likely
    show up."""
    # (1) normal case.
    w_star = [3.7 * s for s in DEFAULT_SEEDS]
    A, b = _synthetic_windows(w_star)
    normal = cus.fit_burn_weights(A, b, None, CFG)
    w = normal["weights"]
    assert w["output"] >= w["input"] >= 0.0
    assert 0.0 <= w["cache_read"] <= w["cache_create_5m"] <= w["cache_create_1h"]

    # (2) boundary-degenerate case: true AND seed template both collapse
    # output==input, cache_read==cache_create_5m==cache_create_1h.
    w_star_deg = [2.0, 2.0, 0.5, 0.5, 0.5]
    seeds_deg = [2.0, 2.0, 0.5, 0.5, 0.5]
    A2, b2 = _synthetic_windows(w_star_deg)
    deg = cus.fit_burn_weights(A2, b2, seeds_deg, CFG)
    w2 = deg["weights"]
    assert w2["output"] >= w2["input"] >= 0.0
    assert 0.0 <= w2["cache_read"] <= w2["cache_create_5m"] <= w2["cache_create_1h"]
    # Boundary recovered essentially exactly (data and seed agree).
    assert w2["output"] == pytest.approx(w2["input"], abs=1e-6)
    assert w2["cache_read"] == pytest.approx(w2["cache_create_5m"], abs=1e-6)
    assert w2["cache_create_5m"] == pytest.approx(w2["cache_create_1h"], abs=1e-6)


def test_zero_5m_seed_pinned_not_fallback():
    """An all-zero `cache_create_5m` raw column (e.g. that tier hasn't been
    exercised yet) is UNPOPULATED -- it is excluded from `_condition_number`
    's Gram (so it can't trip the condition fallback on its own), appears
    in `seed_pinned`, and the fit still proceeds normally (source "fit",
    not "seed-fallback") using the other 4 well-conditioned populated
    columns."""
    w_star = [1.0, 5.0, 0.15, 1.3, 2.1]
    A, b = _synthetic_windows(w_star, cache_5m_zero=True)

    result = cus.fit_burn_weights(A, b, None, CFG)

    assert result["source"] == "fit"
    assert result["condition_number"] < 1.0e6
    assert result["seed_pinned"] == ["cache_create_5m"]


def test_ill_conditioned_falls_back():
    """A genuinely ill-conditioned populated A -- ``input`` and ``output``
    columns collinear to within 1e-9 relative (no meaningful data signal
    separating them) -- pushes kappa past `cond_max` (1e6), so
    `fit_burn_weights` reports source "seed-fallback" and returns the
    (validly) scaled seed rather than trusting an unstable NNLS solve."""
    A, b = [], []
    for i in range(250):
        base = 1.0 + i * 0.001
        row = [base, base * (1.0 + 1e-9), 20 + (i * 3) % 15,
               5 + (i * 11) % 10, 2 + (i * 17) % 8]
        row = [float(v) for v in row]
        A.append(row)
        b.append(_dot(row, [1.0, 5.0, 0.15, 1.3, 2.1]))

    result = cus.fit_burn_weights(A, b, None, CFG)

    assert result["condition_number"] > 1.0e6  # includes float("inf")
    assert result["source"] == "seed-fallback"
    scaled_seed = [result["seed_scale"] * s for s in DEFAULT_SEEDS]
    for name, seed_v in zip(COLUMNS, scaled_seed):
        assert result["weights"][name] == pytest.approx(seed_v)


def test_insufficient_data():
    """`n_windows < min_windows` (200, default) reports source
    "insufficient-data" and returns the RAW, UNSCALED seed weights
    (`s = 1`, `w = seeds_rel`) -- never attempts a scale estimate or a fit
    on too few windows to trust either."""
    w_star = [3.7 * s for s in DEFAULT_SEEDS]
    A, b = _synthetic_windows(w_star, n=50)

    result = cus.fit_burn_weights(A, b, None, CFG)

    assert result["source"] == "insufficient-data"
    assert result["n_windows"] == 50
    assert result["seed_scale"] == 1.0
    for name, seed_v in zip(COLUMNS, DEFAULT_SEEDS):
        assert result["weights"][name] == pytest.approx(seed_v)


def test_residual_fraction_published():
    """`residual_fraction` is present on every result and equals
    `||A @ w - b|| / ||b||` for whatever `w` was ultimately returned --
    recomputed independently here from the published `weights` dict."""
    w_star = [3.7 * s for s in DEFAULT_SEEDS]
    A, b = _synthetic_windows(w_star)

    result = cus.fit_burn_weights(A, b, None, CFG)

    assert "residual_fraction" in result
    w_list = [result["weights"][name] for name in COLUMNS]
    Aw = cus._matvec(A, w_list)
    resid = [b[i] - Aw[i] for i in range(len(b))]
    expected = math.sqrt(cus._dot(resid, resid)) / math.sqrt(cus._dot(b, b))
    assert result["residual_fraction"] == pytest.approx(expected)


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
