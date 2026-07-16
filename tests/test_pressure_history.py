"""Task 27b (spec-2 token-pressure forecaster, STAGE 1): persisted
cross-cycle rolling-history accumulator -- two pressure-owned rolling
stores under ``PRESSURE_ROOT`` (NEVER state.json, NEVER `save_state`, G0)
that close a real gap Tasks 14/16 (weight-fit) and 12 (trend) left open:
neither `pressure_cmd` (Task 21) nor `_pressure_cycle` (Task 23) has ever
had a real weight-window producer wired in -- both always called
``_build_weight_windows([], [], [])`` literally -- so `fit_burn_weights`
could never see >= ``weight_refit.min_windows`` (200) real windows (source
stuck at "insufficient-data" forever), and `_pressure_build_session_table`
always fed `_trend_class` a single-sample history (stuck at its own
conservative ``len(rate_history) < 3`` -> "steady" default forever).

Under test (all in ``cus.py``):

  weight_windows.jsonl / weight_window_resets.jsonl / weight_window_cursor.json
      -- `_pressure_weight_window_append`/`_pressure_load_weight_windows`,
      `_pressure_weight_reset_append`/`_pressure_load_weight_resets`,
      `_pressure_save_weight_window_cursor`/`_pressure_load_weight_window_cursor`.
  session_rate_history.jsonl -- `_pressure_rate_history_append`/
      `_pressure_load_rate_history`/`_pressure_rate_history_by_session`.
  `_pressure_raw_token_totals_by_account` -- raw (unweighted) per-token-type
      totals per account from this cycle's transcript tails (the
      `token_totals_per_window` ingredient `_build_weight_windows`, Task 14,
      needs and nothing before this task ever produced).
  `_pressure_window_observations` -- this cycle's new window-observation
      row(s) + detected reset-crossing(s) + updated cursor, diffed against
      the PRIOR cursor (a Δpct window needs two consecutive polls).
  `_pressure_accumulated_weight_windows(state, tails, intervals, config,
      now, *, persist)` -- the accumulated-history counterpart to a bare
      ``_build_weight_windows([], [], [])`` call; `persist=True` (daemon,
      `_pressure_cycle`) appends this cycle's own new observations before
      reading; `persist=False` (on-demand CLI, `pressure_cmd`) reads the
      accumulated stores as they stand and never appends -- mirrors
      `_read_active_tails(..., persist=...)`'s own contract (Task 10)
      exactly, so the CLI can never race the daemon over the same on-disk
      store.
  `_pressure_build_session_table(..., persist=...)` -- same persist gate,
      extended to `_trend_class`'s per-session rate-history input.

ATOMICITY (Task 27b banner in cus.py has the full rationale): every store
is tmp+rename (`atomic_write_bytes`), and a JSONL store's read-modify-write
append is additionally guarded by a companion ``<path>.lock`` file's
`fcntl.flock` -- a reader therefore always sees either the complete prior
content or the complete new content, never a torn write; the tolerant
per-line JSONL reader (mirrors `_pressure_shadow_scan`'s own convention)
skips a malformed/corrupt line without losing any OTHER, already-valid row.

HARNESS (same as every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Run with ``python -m pytest tests/ -q``.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

# reference_x pinned to 5 (the live production pin, FACT #4); one 20x
# account ("A") gives ratio 4.0 -- the same fleet shape every other
# tests/test_pressure_*.py file uses.
BASE_CFG = {
    "capacity_aware": {"enabled": True, "reference_x": 5},
    "thresholds": {"steps": [70, 85, 94]},
    "per_model_weekly": {"cap_pct": 95},
    "accounts": [{"name": "A", "capacity_x": 20}],
}

COLUMNS = cus._PRESSURE_WEIGHT_COLUMNS
DEFAULT_SEEDS = cus._PRESSURE_DEFAULT_SEEDS_REL


def _acct(pct=50.0, pct7d=10.0):
    return {"capacity_x": 20, "current_5h_pct": pct, "current_7d_pct": pct7d,
            "last_poll_ts": NOW.isoformat()}


def _env(tmp_path, monkeypatch):
    """Isolated tmp tree for every Task 27b store, mirroring
    `tests/test_pressure_shadow.py`'s own `_env` (`_pressure_cycle`'s own
    harness) -- `CLAUDE_DIR`/`SESSIONS_LOG` for the Phase-D I/O
    `_pressure_cycle` performs internally, `ACCOUNTS_DIR`/`PRESSURE_JSON`/
    `PRESSURE_ROOT` for every pressure-owned on-disk artifact including
    this task's new stores, and a fresh in-memory `_PRESSURE_TAIL_OFFSETS`
    per test (Task 10: caller-owned, in-memory only)."""
    monkeypatch.setattr(cus, "CLAUDE_DIR", tmp_path / "claude_home")
    monkeypatch.setattr(cus, "SESSIONS_LOG", tmp_path / "sessions.log")

    accounts_dir = tmp_path / "claude-accounts"
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(cus, "PRESSURE_JSON", accounts_dir / "pressure.json")
    monkeypatch.setattr(cus, "PRESSURE_ROOT", accounts_dir / "pressure")

    monkeypatch.setattr(cus, "_PRESSURE_TAIL_OFFSETS", {})

    return accounts_dir


def _cli_env(tmp_path, monkeypatch):
    """Isolated tmp tree for the `cus pressure` CLI (Task 21's
    `pressure_cmd`), matching `tests/test_pressure_cli.py`'s own `_env`
    (including this task's hermeticity fix): `STATE_JSON`/`CONFIG_YAML` on
    disk (the CLI loads both itself, unlike `_pressure_cycle`), plus every
    pressure-owned path `_env` above isolates."""
    accounts_dir = _env(tmp_path, monkeypatch)

    state = {"accounts": {"A": _acct()}}
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    monkeypatch.setattr(cus, "STATE_JSON", state_path)

    config_path = tmp_path / "config.yaml"
    cus.write_yaml(config_path, BASE_CFG)
    monkeypatch.setattr(cus, "CONFIG_YAML", config_path)

    return accounts_dir


def _synthetic_windows(w_true, n=250, start=NOW):
    """A well-conditioned, deterministic (no RNG) synthetic fleet of ``n``
    accumulated weight-window store rows for account "A": 5 diverse
    token-mix columns via modular arithmetic (never proportional to each
    other, so the populated-column Gram is well-conditioned -- the SAME
    recipe `tests/test_fit_weights.py::_synthetic_windows` uses and proves
    reaches source "fit"), one-minute-duration windows (below both the
    cache_create_5m/1h TTLs, so `_build_weight_windows`'s expiry
    correction is a no-op and the stored raw totals pass through to `A`
    unchanged), and ``pct_end - pct_start`` set so the resulting ``b``
    exactly equals ``dot(row, w_true)`` (ratio pinned to 1.0)."""
    rows = []
    for i in range(n):
        row = [
            50 + (i * 7) % 40,
            30 + (i * 13) % 25,
            20 + (i * 3) % 15,
            5 + (i * 11) % 10,
            2 + (i * 17) % 8,
        ]
        row = [float(v) for v in row]
        b_i = sum(x * w for x, w in zip(row, w_true))
        win_start = start + timedelta(minutes=2 * i)
        win_end = win_start + timedelta(minutes=1)
        rows.append({
            "account": "A",
            "start_ts": win_start.isoformat(),
            "end_ts": win_end.isoformat(),
            "pct_start": 10.0,
            "pct_end": 10.0 + b_i * 100.0,
            "ratio": 1.0,
            "input": row[0], "output": row[1], "cache_read": row[2],
            "cache_create_5m": row[3], "cache_create_1h": row[4],
        })
    return rows


def test_window_history_accumulates_across_cycles(tmp_path, monkeypatch):
    """Appending N cycles' worth of window observations (in separate
    append calls, simulating separate daemon cycles) accumulates: a
    read-back yields exactly N rows. Driven far enough (250 rows, a
    well-conditioned synthetic fleet), `_build_weight_windows` over the
    accumulated store yields >= `weight_refit.min_windows` (200, the
    default) rows and `fit_burn_weights`'s source becomes "fit" -- the gap
    this task closes: before Task 27b this was unreachable (the call site
    always passed literal empty lists)."""
    _env(tmp_path, monkeypatch)

    w_true = [3.7 * s for s in DEFAULT_SEEDS]
    all_rows = _synthetic_windows(w_true, n=250)

    # Append in 10 separate batches of 25 -- "N cycles", not one write.
    for i in range(0, 250, 25):
        cus._pressure_weight_window_append(all_rows[i:i + 25])

    stored = cus._pressure_load_weight_windows()
    assert len(stored) == 250

    A, b, dropped = cus._pressure_accumulated_weight_windows(
        {"accounts": {}}, {}, {}, BASE_CFG, NOW, persist=False)

    assert dropped == {}
    assert len(A) == 250
    assert len(b) == 250

    weight_fit = cus.fit_burn_weights(A, b, None, BASE_CFG)
    assert weight_fit["n_windows"] == 250
    assert weight_fit["source"] == "fit"


def test_trend_becomes_rising_from_history(tmp_path, monkeypatch):
    """Accumulated per-session rate samples with increasing gaps (positive
    second differences, i.e. genuine acceleration) make `_trend_class`
    read "rising" over the accumulated history -- unreachable before this
    task (the caller always fed a single-sample history, which
    `_trend_class`'s own `len(rate_history) < 3` rule pins to "steady")."""
    _env(tmp_path, monkeypatch)

    rates = [0.0, 1.0, 3.0, 6.0, 10.0]  # gaps 1,2,3,4 -- accelerating
    rows = [
        {"session_id": "s1", "rate": r, "ts": (NOW + timedelta(minutes=i)).isoformat()}
        for i, r in enumerate(rates)
    ]
    cus._pressure_rate_history_append(rows, NOW + timedelta(minutes=4))

    history = cus._pressure_rate_history_by_session(cus._pressure_load_rate_history())
    assert history["s1"] == rates

    pressure_cfg = BASE_CFG.get("pressure", {}) or {}
    accel_thresh = float(pressure_cfg.get("trend_accel_thresh", 0.02))
    trend = cus._trend_class(history["s1"], accel_thresh)
    assert trend == "rising"


def test_history_bounded(tmp_path, monkeypatch):
    """Bounded growth on both stores: the weight-window store is
    count-bounded (`_PRESSURE_WEIGHT_WINDOW_MAX_ROWS`) -- appending beyond
    the bound prunes the OLDEST rows first, keeping the store at exactly
    the bound; the rate-history store is AGE-bounded
    (`_PRESSURE_RATE_HISTORY_MAX_AGE_MIN`) -- a sample older than the
    trailing window is pruned on the next append regardless of count."""
    _env(tmp_path, monkeypatch)

    max_rows = cus._PRESSURE_WEIGHT_WINDOW_MAX_ROWS
    extra = 500
    rows = [
        {"account": "A", "start_ts": NOW.isoformat(), "end_ts": NOW.isoformat(),
         "pct_start": 0.0, "pct_end": 1.0, "ratio": 1.0, "idx": i,
         "input": 1.0, "output": 1.0, "cache_read": 1.0,
         "cache_create_5m": 1.0, "cache_create_1h": 1.0}
        for i in range(max_rows + extra)
    ]
    cus._pressure_weight_window_append(rows)

    stored = cus._pressure_load_weight_windows()
    assert len(stored) == max_rows
    # Oldest `extra` rows pruned -- the surviving rows are the MOST RECENT
    # `max_rows` (highest idx values), in original order.
    assert [r["idx"] for r in stored] == list(range(extra, max_rows + extra))

    # Rate history: one old (stale) sample + one fresh sample. Appending
    # the fresh one prunes the stale one on the SAME append (age-based, not
    # count-based -- the store never even briefly holds both).
    stale_ts = NOW - timedelta(minutes=cus._PRESSURE_RATE_HISTORY_MAX_AGE_MIN + 30)
    cus._pressure_rate_history_append(
        [{"session_id": "s1", "rate": 1.0, "ts": stale_ts.isoformat()}], stale_ts)
    cus._pressure_rate_history_append(
        [{"session_id": "s1", "rate": 2.0, "ts": NOW.isoformat()}], NOW)

    rate_rows = cus._pressure_load_rate_history()
    assert len(rate_rows) == 1
    assert rate_rows[0]["rate"] == 2.0


def test_cli_reads_readonly_no_append(tmp_path, monkeypatch):
    """The on-demand CLI path (`cus pressure --json`, persist=False) reads
    the accumulated stores WITHOUT appending -- the store is byte-identical
    before and after invoking the command. The daemon path
    (`_pressure_cycle`, persist=True) DOES append -- contrasting the two
    persist gates on the same store."""
    _cli_env(tmp_path, monkeypatch)

    seed_rows = [
        {"account": "A", "start_ts": NOW.isoformat(), "end_ts": NOW.isoformat(),
         "pct_start": 0.0, "pct_end": 1.0, "ratio": 1.0,
         "input": 1.0, "output": 1.0, "cache_read": 1.0,
         "cache_create_5m": 1.0, "cache_create_1h": 1.0}
        for _ in range(3)
    ]
    cus._pressure_weight_window_append(seed_rows)
    store_path = cus._pressure_weight_window_path()
    before = store_path.read_bytes()

    result = CliRunner().invoke(cus.cli, ["pressure", "--json"])
    assert result.exit_code == 0, result.output

    after = store_path.read_bytes()
    assert after == before

    # Same read-only guarantee for the rate-history store: the CLI's own
    # session-table build (persist=False) must not append either.
    rate_path = cus._pressure_rate_history_path()
    rate_before = rate_path.read_bytes() if rate_path.exists() else b""
    result2 = CliRunner().invoke(cus.cli, ["pressure", "--json"])
    assert result2.exit_code == 0, result2.output
    rate_after = rate_path.read_bytes() if rate_path.exists() else b""
    assert rate_after == rate_before

    # Contrast: the daemon path DOES append. A prior cursor must exist for
    # a Δpct window to be produced (the very first cycle only seeds it),
    # so run two cycles.
    monkeypatch.setattr(
        cus, "_pressure_raw_token_totals_by_account",
        lambda tails, intervals: {"A": {"input": 10.0, "output": 10.0, "cache_read": 10.0,
                                         "cache_create_5m": 10.0, "cache_create_1h": 10.0}},
    )
    state = {"accounts": {"A": _acct(pct=50.0)}}
    cus._pressure_cycle(state, BASE_CFG, NOW)
    state2 = {"accounts": {"A": _acct(pct=55.0)}}
    cus._pressure_cycle(state2, BASE_CFG, NOW + timedelta(minutes=5))

    grown = store_path.read_bytes()
    assert grown != before
    assert len(cus._pressure_load_weight_windows()) == 3 + 1


def test_atomic_append(tmp_path, monkeypatch):
    """Concurrent-safe append (a companion `<path>.lock` guards the
    read-modify-write, the actual write is tmp+rename): a malformed/
    partial line already present in the store does not corrupt the prior
    VALID rows, and does not prevent a subsequent append from succeeding."""
    _env(tmp_path, monkeypatch)

    good_row_1 = {"account": "A", "start_ts": "t0", "end_ts": "t1",
                  "pct_start": 0.0, "pct_end": 1.0, "ratio": 1.0,
                  "input": 1.0, "output": 1.0, "cache_read": 1.0,
                  "cache_create_5m": 1.0, "cache_create_1h": 1.0}
    good_row_2 = dict(good_row_1, start_ts="t1", end_ts="t2")

    store_path = cus._pressure_weight_window_path()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    # Hand-write: two valid JSONL lines + one malformed/partial line, as if
    # a prior crash had torn the very last line of a non-atomic append.
    lines = [
        json.dumps(good_row_1, separators=(",", ":")),
        json.dumps(good_row_2, separators=(",", ":")),
        '{"account": "A", "start_ts": "t2", "end_ts"',  # malformed/partial
    ]
    store_path.write_text("\n".join(lines) + "\n")

    new_row = dict(good_row_1, start_ts="t3", end_ts="t4")
    cus._pressure_weight_window_append([new_row])

    stored = cus._pressure_load_weight_windows()
    assert len(stored) == 3  # 2 prior valid rows + 1 newly appended
    assert stored[0]["start_ts"] == "t0"
    assert stored[1]["start_ts"] == "t1"
    assert stored[2]["start_ts"] == "t3"

    # The lock file exists alongside the data file (the concurrency guard),
    # never the data file itself being flock'd.
    lock_path = store_path.parent / (store_path.name + ".lock")
    assert lock_path.exists()


def test_cycle_appends_and_fit_uses_history(tmp_path, monkeypatch):
    """Integration: after `_pressure_cycle` runs across several synthetic
    cycles, the weight-fit input it used is the ACCUMULATED store, not an
    always-empty literal -- the store grows across cycles, and a
    read-only, out-of-band read of the accumulated store (persist=False)
    matches what the daemon's own last snapshot reports as `n_windows`."""
    _env(tmp_path, monkeypatch)

    monkeypatch.setattr(
        cus, "_pressure_raw_token_totals_by_account",
        lambda tails, intervals: {"A": {"input": 10.0, "output": 10.0, "cache_read": 10.0,
                                         "cache_create_5m": 10.0, "cache_create_1h": 10.0}},
    )

    pct_series = [10.0, 12.0, 15.0, 19.0, 24.0]  # strictly increasing -> positive b each cycle
    snapshot = None
    for i, pct in enumerate(pct_series):
        state = {"accounts": {"A": _acct(pct=pct)}}
        snapshot = cus._pressure_cycle(state, BASE_CFG, NOW + timedelta(minutes=5 * i))

    # 5 cycles -> cycle 1 only seeds the cursor (no window yet); cycles
    # 2-5 each produce one Δpct window row against the PRIOR cycle's poll.
    stored = cus._pressure_load_weight_windows()
    assert len(stored) == 4

    A, b, dropped = cus._pressure_accumulated_weight_windows(
        {"accounts": {}}, {}, {}, BASE_CFG, NOW + timedelta(minutes=5 * len(pct_series)),
        persist=False)
    assert dropped == {}
    assert len(A) == 4

    assert snapshot["weight_fit"]["n_windows"] == 4


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
