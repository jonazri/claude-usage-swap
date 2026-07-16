"""Task 12 (spec-2 token-pressure forecaster, STAGE 1, Phase D): per-session
trailing burn RATE + trend/acceleration + deterministic session class (§3
"Per-session trailing rate" / "Classification", §5.2 targeting walk).

Covers ``_session_rate``, ``_trend_class``, ``_classify_session`` -- the
three pure functions Task 12 introduces. This rate feeds per-session
attribution-SHARE and targeting candidacy (§5.2) ONLY -- never the forecast
itself, which stays on cus's own coarse per-account ``current_%``/
``burn_rate_*_pct_per_min`` from ``state.json`` (unaffected by this file).

HARNESS (reused by every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Add the repo root to ``sys.path`` and ``import cus``. Run with ``python -m
pytest tests/ -q`` (same command CI uses); a single file runs standalone via
``python tests/test_pressure_rate.py``.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# --------------------------------------------------------------------------
# _session_rate: window = max(60, min(session_age_s, rate_window_min*60))
# --------------------------------------------------------------------------


def test_rate_floor_60s():
    """A 20s-old session floors the divisor at 60s (1 min), never 20s --
    otherwise a tiny early sample would extrapolate to an absurd %/min."""
    # sum=60, window floored to 60s == 1 min -> rate == sum/1 == 60.0.
    assert cus._session_rate([60.0], 20, 10) == pytest.approx(60.0)
    # Multiple samples: the function sums them, not just the first element.
    assert cus._session_rate([20.0, 40.0], 20, 10) == pytest.approx(60.0)


def test_rate_caps_at_rate_window():
    """A 3h-old session caps the divisor at rate_window_min*60 (600s = 10
    min at the default), never averaging over its whole lifetime."""
    three_hours_s = 3 * 3600
    assert cus._session_rate([100.0], three_hours_s, 10) == pytest.approx(10.0)


def test_rate_min_age_between():
    """A 5-min-old (300s) session divides by its own actual age (300s = 5
    min) -- between the 60s floor and the rate_window cap, neither clamp
    fires."""
    assert cus._session_rate([25.0], 300, 10) == pytest.approx(5.0)


def test_rate_empty_samples_is_zero():
    """No weighted samples this window -> rate 0.0, never a ZeroDivisionError
    or a stale carried-over value."""
    assert cus._session_rate([], 300, 10) == pytest.approx(0.0)


# --------------------------------------------------------------------------
# _trend_class: discrete second-difference acceleration vs +-accel_thresh
# --------------------------------------------------------------------------


def test_trend_rising_is_flagged():
    """A convex-increasing rate history (each successive delta bigger than
    the last -- a fast-ramping fan-out, design doc §3) has positive mean
    acceleration and is flagged 'rising', not just 'steady' growth."""
    rate_history = [1.0, 2.0, 4.0, 8.0, 16.0]
    assert cus._trend_class(rate_history, accel_thresh=0.5) == "rising"


def test_trend_falling_is_flagged():
    """A rate history whose successive drops keep getting BIGGER (an
    accelerating decline, e.g. a throttle taking hold) has negative mean
    acceleration -> 'falling'. (Mirroring the 'rising' test's growing
    deltas [1,2,4,8] with growing drops [-1,-2,-4,-8] instead -- NOT simply
    reversing the rising sequence, which decelerates towards zero and is
    itself 'rising' by the same second-difference math.)"""
    rate_history = [20.0, 19.0, 17.0, 13.0, 5.0]
    assert cus._trend_class(rate_history, accel_thresh=0.5) == "falling"


def test_trend_steady_default():
    """A linear (constant-slope) rate history has ~zero second difference
    -- steady growth, not accelerating -- so it reads 'steady', and fewer
    than 3 points (not enough to observe a second derivative at all) is
    the same conservative 'steady' default."""
    assert cus._trend_class([1.0, 2.0, 3.0, 4.0, 5.0], accel_thresh=0.5) == "steady"
    assert cus._trend_class([1.0, 2.0], accel_thresh=0.5) == "steady"
    assert cus._trend_class([], accel_thresh=0.5) == "steady"


# --------------------------------------------------------------------------
# _classify_session: deterministic heuristics off a single session's own
# record (no coordinator rollup -- that's Task 13).
# --------------------------------------------------------------------------


def test_classify_human_paced_is_interactive():
    """LOAD-BEARING (§5.2): a human-paced session -- real, sustained
    activity, but no structural automation signal and a rate no automation
    heuristic fires on -- classifies 'interactive', and is therefore NEVER
    a targeting candidate."""
    record = {
        "rate_pct_per_min": 0.8,
        "sample_count": 12,
        "cwd": "/home/yaz/project",
    }
    assert cus._classify_session(record) == "interactive"


def test_classify_idle_no_activity():
    """No samples in the window -> 'idle', regardless of any other field."""
    record = {"rate_pct_per_min": 0.0, "sample_count": 0, "cwd": "/home/yaz/project"}
    assert cus._classify_session(record) == "idle"


def test_classify_subagent_heavy():
    """This session's OWN dir carries nested subagent transcripts (Task 10
    FACT #6: `<slug>/<session_id>/subagents/agent-*.jsonl`) -> the most
    specific elastic class, 'subagent-heavy'."""
    record = {
        "rate_pct_per_min": 3.0,
        "sample_count": 8,
        "cwd": "/home/yaz/project",
        "has_subagent_children": True,
    }
    assert cus._classify_session(record) == "subagent-heavy"


def test_classify_workflow_children():
    """This session's OWN dir carries nested workflow transcripts
    (`<slug>/<session_id>/workflows/*.jsonl`) -> 'workflow'."""
    record = {
        "rate_pct_per_min": 2.0,
        "sample_count": 8,
        "cwd": "/home/yaz/project",
        "has_workflow_children": True,
    }
    assert cus._classify_session(record) == "workflow"


def test_classify_committee_loop_worktree_cwd():
    """cwd sits under a `.worktrees/<child>` layout (design doc §3
    Coordinator rollup / committee #9's observed convention) -> the
    parallel-worktree-per-agent pattern, 'committee-loop' -- PROVIDED it is
    also corroborated as sustained automation (final-review fix-wave, §5.2
    SAFETY hole: a bare worktree cwd is no longer sufficient on its own,
    see `test_human_paced_worktree_cwd_not_elastic` below). This fixture
    carries a matured ``session_age_s`` (past the ``rate_window_min * 60``
    floor) as its corroboration signal -- the SAME gate the generic-rate
    fallback (heuristic 5) uses -- so it still asserts the intended class
    for a genuine, sustained automated committee-loop session."""
    record = {
        "rate_pct_per_min": 2.0,
        "sample_count": 8,
        "cwd": "/home/yaz/code/misc/claude-usage-swap/.worktrees/spec2-stage1",
        "session_age_s": 900.0,
        "rate_window_min": 10,
    }
    assert cus._classify_session(record) == "committee-loop"


def test_human_paced_worktree_cwd_not_elastic():
    """LOAD-BEARING (final-review fix-wave, §5.2 SAFETY hole): this
    project's OWN interactive dev sessions run `claude` directly with a cwd
    of `<repo>/.claude/worktrees/<name>` -- exactly the layout
    `_in_worktree_cwd` matches. Before this fix, ANY session in such a cwd
    classified 'committee-loop' (elastic) with no rate/corroboration gate --
    a HUMAN working interactively in a worktree checkout was misclassified
    elastic and became a §5.2 targeting candidate. A worktree cwd ALONE
    must never be sufficient: this record is human-paced (a real,
    sustained-looking rate below the automation threshold), has no
    structural child signal, and is uncorroborated (young age, no rising
    trend) -- so it must classify 'interactive', NEVER 'committee-loop'."""
    record = {
        "rate_pct_per_min": 0.8,
        "sample_count": 12,
        "cwd": "/home/yaz/code/misc/claude-usage-swap/.claude/worktrees/spec2-stage1",
        "session_age_s": 30.0,
        "rate_window_min": 10,
        "trend": "steady",
    }
    assert cus._classify_session(record) == "interactive"


def test_corroborated_worktree_is_committee_loop():
    """The flip side of the SAFETY fix above (final-review fix-wave): a
    GENUINE automated committee-loop session in a worktree cwd -- one whose
    rate IS corroborated as sustained automation, via the same trend/age
    gate heuristic 5 uses -- still classifies 'committee-loop'. Two
    independent corroboration signals, either sufficient on its own:
      (a) a genuinely rising rate trend, even while still young; or
      (b) the session is old enough that its rate reading is no longer
          riding `_session_rate`'s 60s age floor.
    """
    rising_trend_young_age = {
        "rate_pct_per_min": 2.0,
        "sample_count": 8,
        "cwd": "/home/yaz/code/misc/claude-usage-swap/.claude/worktrees/spec2-stage1",
        "session_age_s": 30.0,
        "rate_window_min": 10,
        "trend": "rising",
    }
    assert cus._classify_session(rising_trend_young_age) == "committee-loop"

    steady_trend_old_age = {
        "rate_pct_per_min": 2.0,
        "sample_count": 8,
        "cwd": "/home/yaz/code/misc/claude-usage-swap/.claude/worktrees/spec2-stage1",
        "session_age_s": 601.0,
        "rate_window_min": 10,
        "trend": "steady",
    }
    assert cus._classify_session(steady_trend_old_age) == "committee-loop"


def test_classify_high_rate_without_structure_falls_back_to_workflow():
    """No structural signal (no children, no worktree cwd) but a rate no
    human typing could plausibly sustain -- design doc §3 lists 'a high
    sustained rate' as its own elastic signal -- falls back to the generic
    automation class 'workflow' rather than 'interactive', PROVIDED the
    rate reading is corroborated as sustained (Task 24b hardening): here
    via ``session_age_s`` already past the rate window's own maturity
    floor (``rate_window_min * 60`` = 600s), so this is not a fixture that
    relied on the old, now-unsafe rate-only-no-corroboration behavior."""
    record = {
        "rate_pct_per_min": 9.0,
        "sample_count": 20,
        "cwd": "/home/yaz/project",
        "session_age_s": 900.0,
        "rate_window_min": 10,
    }
    assert cus._classify_session(record) == "workflow"


def test_young_single_burst_not_elastic():
    """Task 24b (pre-shadow-flip HARD gate, §5.2 'never target a human'):
    a brand-new *interactive* session whose first turn is one big
    paste/tool-result can spike the instantaneous rate
    (``_session_rate``'s 60s age-floor divisor) over
    ``_CLASSIFY_HIGH_RATE_PCT_PER_MIN`` without being remotely automated.
    With NO structural signal and NO corroboration -- trend not
    'rising' and the session still younger than its own rate window's
    maturity floor (``rate_window_min * 60``) -- the rate-only fallback
    must NOT fire; this must classify 'interactive', never 'workflow'
    (a false 'elastic' label here would make this session a dry-run
    throttle target -- unsafe)."""
    uncorroborated_records = [
        # No trend field at all, young age (well under the 600s floor).
        {
            "rate_pct_per_min": 9.0,
            "sample_count": 1,
            "cwd": "/home/yaz/project",
            "session_age_s": 20.0,
            "rate_window_min": 10,
        },
        # Explicit steady trend, young age.
        {
            "rate_pct_per_min": 9.0,
            "sample_count": 1,
            "cwd": "/home/yaz/project",
            "session_age_s": 45.0,
            "rate_window_min": 10,
            "trend": "steady",
        },
        # No age/rate_window_min fields at all (record shape the on-demand
        # single-sample build path can hand in) -- must default
        # conservatively, never treat missing age as "old enough".
        {
            "rate_pct_per_min": 9.0,
            "sample_count": 1,
            "cwd": "/home/yaz/project",
        },
    ]
    for record in uncorroborated_records:
        assert cus._classify_session(record) == "interactive", record


def test_corroborated_high_rate_is_workflow():
    """Task 24b: the SAME high, structurally-unsupported rate DOES class
    'workflow' once genuinely corroborated as sustained automation --
    either signal alone suffices:
      (a) a genuinely rising multi-window trend (`_trend_class` over ~1h
          of points), even while the session is still young; or
      (b) the session is old enough that its rate is no longer riding
          `_session_rate`'s 60s age floor (``session_age_s >=
          rate_window_min * 60``), even with a 'steady' trend.
    """
    rising_trend_young_age = {
        "rate_pct_per_min": 9.0,
        "sample_count": 1,
        "cwd": "/home/yaz/project",
        "session_age_s": 30.0,
        "rate_window_min": 10,
        "trend": "rising",
    }
    assert cus._classify_session(rising_trend_young_age) == "workflow"

    steady_trend_old_age = {
        "rate_pct_per_min": 9.0,
        "sample_count": 20,
        "cwd": "/home/yaz/project",
        "session_age_s": 601.0,
        "rate_window_min": 10,
        "trend": "steady",
    }
    assert cus._classify_session(steady_trend_old_age) == "workflow"


def test_classify_missing_fields_is_conservative():
    """An empty record (no rate, no samples, no cwd) has nothing to key an
    elastic class on -- 'idle' (no samples), never a guessed elastic
    label."""
    assert cus._classify_session({}) == "idle"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
