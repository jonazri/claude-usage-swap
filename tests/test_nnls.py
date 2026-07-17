"""Task 15 (spec-2 token-pressure forecaster, STAGE 1): vendored pure-Python
NNLS core -- Lawson-Hanson (1974) active-set NNLS with a Householder-QR
passive-set inner solve. Pure Python (FACT #8/G4 -- NO numpy/scipy anywhere
in ``cus.py`` itself; ``scipy`` appears ONLY as an optional oracle in this
test file, guarded by ``pytest.importorskip``), and DETERMINISTIC end to end
-- the same ``(M, y)`` must produce byte-identical ``x`` across repeated
calls, since the burn-weight fit (Task 16) feeds the §9.2 golden-replay
backtest, which requires exact reproducibility across runs and machines.

Interface under test (``cus.py``):

    _nnls(M, y, *, max_iter) -> (x, converged)
        # min ||M x - y||^2 s.t. x >= 0. M is a list of rows (each a list
        # of floats), y a list of floats, x a list of floats.
    _householder_qr_lstsq(A, b) -> x
        # unconstrained least squares via Householder QR (never normal
        # equations -- that would square the condition number).

HARNESS (same as every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Run with ``python -m pytest tests/ -q``.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def test_nnls_recovers_positive_solution():
    # Well-conditioned 6x3 system built FROM a known all-positive x* so the
    # unconstrained LS solution and the NNLS solution coincide exactly --
    # the active-set machinery should never need to clamp anything here.
    M = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
        [1.0, 1.0, 0.0],
        [0.0, 1.0, 1.0],
        [1.0, 0.0, 1.0],
    ]
    x_star = [2.0, 3.0, 5.0]
    y = [sum(row[j] * x_star[j] for j in range(3)) for row in M]

    x, converged = cus._nnls(M, y, max_iter=75)

    assert converged is True
    assert len(x) == 3
    for got, want in zip(x, x_star):
        assert abs(got - want) < 1e-8


def test_nnls_clamps_negative_to_zero():
    # Classic textbook NNLS example (Lawson & Hanson): the unconstrained LS
    # solution for this system has a negative second component, so the
    # constrained solution must clamp it to exactly 0 and re-solve the
    # remaining passive column(s) against y.
    M = [
        [1.0, 1.0],
        [1.0, -1.0],
        [0.0, 1.0],
    ]
    y = [2.0, 4.0, -2.0]

    # Unconstrained LS reference (verified by hand / via the QR helper) has
    # x2 < 0 -- confirm that premise so this test actually exercises
    # clamping rather than coincidentally passing.
    unconstrained = cus._householder_qr_lstsq(M, y)
    assert unconstrained[1] < 0.0

    x, converged = cus._nnls(M, y, max_iter=75)

    assert converged is True
    assert x[1] == 0.0
    # With column 2 pinned to zero, column 1 alone solves min||M[:,0]*x1-y||^2.
    col0 = [row[0] for row in M]
    expected_x1 = sum(col0[i] * y[i] for i in range(len(y))) / sum(c * c for c in col0)
    assert abs(x[0] - expected_x1) < 1e-8
    assert expected_x1 >= 0.0


def test_nnls_deterministic():
    # A tie-prone input: two columns are identical, so the dual/gradient
    # vector ties exactly at every step that could select between them --
    # the lowest-index tie-break rule must make every repeated call select
    # the SAME column, giving byte-identical output.
    M = [
        [1.0, 1.0, 0.0],
        [2.0, 2.0, 1.0],
        [0.0, 0.0, 1.0],
        [3.0, 3.0, 2.0],
    ]
    y = [1.0, 3.0, 1.0, 4.0]

    results = [cus._nnls(M, y, max_iter=75) for _ in range(20)]
    first_x, first_converged = results[0]
    first_repr = repr(first_x)

    for x, converged in results[1:]:
        assert repr(x) == first_repr
        assert x == first_x
        assert converged == first_converged


def test_nnls_recovers_under_moderate_conditioning():
    # Guards the condition-squaring fix (committee review of b77c94f): the
    # outer loop's KKT dual/termination test must be computed in RESIDUAL
    # form (`w = M^T (y - M x)`, condition number kappa(M)) rather than via
    # the Gram matrix `M^T M` (which would square kappa for that decision).
    # kappa(M) ~ 8000 here -- moderate collinearity, well below the 1e6
    # fallback threshold, but large enough that a Gram-squared dual
    # (kappa^2 ~ 6.4e7) would be far less trustworthy against the 1e-10
    # relative dual tolerance than the residual-form dual is.
    #
    # M/y are generated OFFLINE (not at test time -- this file stays
    # numpy-free outside the optional scipy oracle) via
    # A = U @ diag([1, 3, 20, 400, 8000]) @ V^T for random orthonormal U, V
    # (an SVD construction with prescribed singular values), so kappa(M) is
    # exact by construction rather than incidental. y = M @ x_star for a
    # KNOWN all-positive x_star, so the NNLS constraint never binds and any
    # correct solver -- constrained or not -- must recover x_star exactly
    # to float precision; this test only exercises whether the dual's
    # variable-selection/termination decision stays correct under real
    # collinearity, not the active-set clamping machinery.
    M = [
        [-548.4515003140713, 113.68639585293795, -475.79100564997935, 755.7635914817755, -184.5101027941619],
        [-2733.4880591556202, 749.7349076098939, -1793.3959574317475, 4891.937705925866, -1427.8156316088948],
        [-833.1616185606476, 196.05783748108547, -609.5771025121087, 1345.4049229286645, -359.0343656614325],
        [255.77547233102592, -34.59693902907729, 261.527903812889, -268.23589290216273, 43.85125429786666],
        [811.830826087431, -204.50315283659887, 588.6486666515443, -1344.7111472668491, 377.5896679685745],
        [899.7678148878737, -265.7175257426638, 569.9848637222673, -1668.3785282819956, 505.2491969652518],
        [-1641.390954173942, 446.7798402540952, -1097.1128433153171, 2904.0578727594648, -844.8771563371064],
        [-730.1725523302129, 155.48116011759595, -621.6862367891049, 1023.8620387828468, -252.95449227541906],
    ]
    y = [287.092833430257, 4556.155074932625, 999.6776026562325, 101.55544372097734, -1039.6558819570657, -1658.21350033915, 2638.1130208795844, 444.29948799076055]
    x_star = [2.0, 5.0, 1.5, 3.0, 4.0]

    x, converged = cus._nnls(M, y, max_iter=75)

    assert converged is True
    assert len(x) == 5
    for got, want in zip(x, x_star):
        assert abs(got - want) < 1e-7


def test_householder_qr_matches_lstsq():
    # Full-rank overdetermined 5x3 system with a known closed-form normal-
    # equations solution (well-conditioned, so normal equations are an
    # acceptable INDEPENDENT reference here even though _nnls itself must
    # never use them).
    M = [
        [1.0, 0.0, 1.0],
        [0.0, 1.0, 1.0],
        [1.0, 1.0, 0.0],
        [2.0, 0.0, 1.0],
        [0.0, 2.0, 1.0],
    ]
    y = [1.0, 2.0, 3.0, 4.0, 5.0]

    x = cus._householder_qr_lstsq(M, y)

    n = 3
    AtA = [[sum(M[i][a] * M[i][b] for i in range(len(M))) for b in range(n)] for a in range(n)]
    Aty = [sum(M[i][a] * y[i] for i in range(len(M))) for a in range(n)]

    def _solve3(A, b):
        # Tiny Gaussian elimination with partial pivoting -- an independent
        # reference solver distinct from the Householder QR under test.
        A = [row[:] for row in A]
        b = list(b)
        n = len(b)
        for col in range(n):
            piv = max(range(col, n), key=lambda r: abs(A[r][col]))
            A[col], A[piv] = A[piv], A[col]
            b[col], b[piv] = b[piv], b[col]
            for r in range(col + 1, n):
                factor = A[r][col] / A[col][col]
                for c in range(col, n):
                    A[r][c] -= factor * A[col][c]
                b[r] -= factor * b[col]
        x = [0.0] * n
        for i in range(n - 1, -1, -1):
            s = b[i] - sum(A[i][j] * x[j] for j in range(i + 1, n))
            x[i] = s / A[i][i]
        return x

    expected = _solve3(AtA, Aty)

    assert len(x) == 3
    for got, want in zip(x, expected):
        assert abs(got - want) < 1e-9


def test_nnls_nonconvergence_flag():
    # A problem with several active-set transitions (5 columns, several
    # forced clamps) starved of iteration budget -- max_iter=1 cannot even
    # complete the first passive-set solve's worth of active-set churn for
    # a problem this size, so it must report converged=False rather than
    # fabricate a partial answer as if it were final.
    M = [
        [4.0, 1.0, 0.0, 0.0, 1.0],
        [1.0, 4.0, 1.0, 0.0, 0.0],
        [0.0, 1.0, 4.0, 1.0, 0.0],
        [0.0, 0.0, 1.0, 4.0, 1.0],
        [1.0, 0.0, 0.0, 1.0, 4.0],
        [2.0, -1.0, 0.0, 0.0, -1.0],
        [-1.0, 2.0, -1.0, 0.0, 0.0],
    ]
    y = [1.0, -2.0, 3.0, -1.0, 2.0, -3.0, 1.0]

    x, converged = cus._nnls(M, y, max_iter=1)

    assert converged is False


def test_nnls_scipy_oracle():
    scipy = pytest.importorskip("scipy")
    from scipy.optimize import nnls as scipy_nnls

    M = [
        [4.0, 1.0, 0.0, 0.0, 1.0],
        [1.0, 4.0, 1.0, 0.0, 0.0],
        [0.0, 1.0, 4.0, 1.0, 0.0],
        [0.0, 0.0, 1.0, 4.0, 1.0],
        [1.0, 0.0, 0.0, 1.0, 4.0],
        [2.0, -1.0, 0.0, 0.0, -1.0],
        [-1.0, 2.0, -1.0, 0.0, 0.0],
    ]
    y = [1.0, -2.0, 3.0, -1.0, 2.0, -3.0, 1.0]

    x, converged = cus._nnls(M, y, max_iter=75)
    assert converged is True

    x_oracle, _residual = scipy_nnls(M, y)

    for got, want in zip(x, x_oracle):
        assert abs(got - want) < 1e-6
