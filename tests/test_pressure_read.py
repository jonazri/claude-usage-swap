"""Task 10 (spec-2 token-pressure forecaster, STAGE 1): bounded reverse-tail
transcript reader — the raw input layer for Phase D (burn attribution).

Read only the trailing tail of each *recently-active* transcript JSONL under
``~/.claude/projects`` (FACT #6) — never the 2.6 GB corpus (§8). Cold start
seeks to ``EOF - byte_cap`` (NEVER byte 0) and walks backward in chunks,
never ``read()``ing a whole large file into memory; steady state resumes at
a caller-supplied offset; truncation (a recorded offset past the file's
current size) falls back to a bounded cold-start read; the per-cycle
aggregate read is capped at ``config['pressure']['tail_bytes_per_cycle']``.

G0-analogous read-only invariant (this task's flavor of it): the on-demand
``--json``/CLI path (``persist=False``) must leave the caller's in-memory
``offsets`` registry byte-identical — only the daemon path (``persist=True``)
advances it — so the CLI and daemon can never race over the same registry.

HARNESS (reused by every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Add the repo root to ``sys.path`` and ``import cus``. Run with ``python -m
pytest tests/ -q`` (same command CI uses); a single file runs standalone via
``python tests/test_pressure_read.py``.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _iso(dt: datetime) -> str:
    """ISO-ms timestamp string matching real transcript lines (FACT #6)."""
    return dt.isoformat().replace("+00:00", "Z")


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for entry in lines:
            fh.write(json.dumps(entry) + "\n")


def _set_mtime(path: Path, dt: datetime) -> None:
    ts = dt.timestamp()
    os.utime(path, (ts, ts))


# Plain default config: rate_window_min=10 (G2) -> TAIL_LOOKBACK_MIN=30 min;
# tail_bytes_per_cycle at the G2 default (64 MiB) so it never binds unless a
# test deliberately shrinks it.
PRESSURE_CFG = {"pressure": {"rate_window_min": 10, "tail_bytes_per_cycle": 64 * 1024 * 1024}}


# ---------------------------------------------------------------------------
# _pressure_transcript_paths: recently-active filter, FACT #6 nested layout
# ---------------------------------------------------------------------------

def test_recently_active_filter(tmp_path):
    """Only transcripts (top-level AND nested subagents/workflows, FACT #6)
    whose mtime is within TAIL_LOOKBACK_MIN (30 min at rate_window_min=10)
    of ``now`` are returned; older ones are excluded."""
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    projects_dir = tmp_path / "projects"

    fresh = projects_dir / "slugA" / "session-fresh.jsonl"
    stale = projects_dir / "slugA" / "session-stale.jsonl"
    sub_fresh = projects_dir / "slugB" / "parent-sid" / "subagents" / "agent-1.jsonl"
    sub_stale = projects_dir / "slugB" / "parent-sid" / "subagents" / "agent-old.jsonl"
    wf_fresh = projects_dir / "slugB" / "parent-sid" / "workflows" / "wf-1.jsonl"

    for p in (fresh, stale, sub_fresh, sub_stale, wf_fresh):
        _write_jsonl(p, [{"timestamp": _iso(now), "sessionId": "x"}])

    _set_mtime(fresh, now)
    _set_mtime(sub_fresh, now)
    _set_mtime(wf_fresh, now)
    _set_mtime(stale, now - timedelta(hours=2))
    _set_mtime(sub_stale, now - timedelta(hours=2))

    result = cus._pressure_transcript_paths(projects_dir, now, rate_window_min=10)

    assert set(result) == {fresh, sub_fresh, wf_fresh}

    # A missing projects_dir is not an error -- just no transcripts yet.
    assert cus._pressure_transcript_paths(tmp_path / "nope", now, rate_window_min=10) == []


# ---------------------------------------------------------------------------
# _reverse_tail_since: cold-start EOF seek, bounded by PER_SESSION_TAIL_BYTES
# ---------------------------------------------------------------------------

def test_cold_start_seeks_to_eof_lookback(tmp_path):
    """A 20+ MB fixture, read cold-start (start_offset=None): only the
    trailing ~PER_SESSION_TAIL_BYTES slice is read, never the whole file."""
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    path = tmp_path / "big.jsonl"

    pad = "x" * 180
    n_lines = 110_000
    lines = []
    for i in range(n_lines):
        if i == 0:
            marker = "FIRST"
        elif i == n_lines - 1:
            marker = "LAST"
        else:
            marker = f"line-{i}"
        lines.append({"timestamp": _iso(now), "marker": marker, "pad": pad})
    _write_jsonl(path, lines)

    file_size = path.stat().st_size
    assert file_size > 20 * 1024 * 1024  # confirm the generated fixture is >20 MB

    since_ts = now - timedelta(days=3650)  # far enough back the timestamp filter never binds

    result_lines, new_offset, reset = cus._reverse_tail_since(
        path, since_ts, start_offset=None, byte_cap=cus.PER_SESSION_TAIL_BYTES)

    assert reset is False
    assert new_offset == file_size
    assert 0 < len(result_lines) < n_lines  # bounded read, not the whole file

    markers = [ln["marker"] for ln in result_lines]
    assert "LAST" in markers
    assert "FIRST" not in markers  # the head of the 20+ MB file was never read

    mid_line_len = len(json.dumps(lines[n_lines // 2]).encode()) + 1  # +1 for '\n'
    approx_expected = cus.PER_SESSION_TAIL_BYTES // mid_line_len
    assert approx_expected * 0.5 <= len(result_lines) <= approx_expected * 1.5


# ---------------------------------------------------------------------------
# _reverse_tail_since: timestamp-bounded reverse seek
# ---------------------------------------------------------------------------

def test_timestamp_bounded_reverse_seek(tmp_path):
    """With byte_cap large enough that it never binds, only lines with
    timestamp >= since_ts come back, oldest-first, chronologically ordered
    (FACT #6)."""
    path = tmp_path / "session.jsonl"
    base = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
    lines = [{"timestamp": _iso(base + timedelta(minutes=i)), "marker": f"m{i}"}
             for i in range(60)]
    _write_jsonl(path, lines)

    since_ts = base + timedelta(minutes=45)  # keep m45..m59 inclusive

    result_lines, new_offset, reset = cus._reverse_tail_since(
        path, since_ts, start_offset=None, byte_cap=1024 * 1024)

    assert reset is False
    assert new_offset == path.stat().st_size
    got_markers = [ln["marker"] for ln in result_lines]
    assert got_markers == [f"m{i}" for i in range(45, 60)]

    # Steady state (start_offset given) never re-reads data before it, even
    # if since_ts would otherwise allow it.
    resume_offset = sum(len(json.dumps(ln).encode()) + 1 for ln in lines[:50])
    result_lines2, new_offset2, reset2 = cus._reverse_tail_since(
        path, base, start_offset=resume_offset, byte_cap=1024 * 1024)
    assert reset2 is False
    assert new_offset2 == path.stat().st_size
    assert [ln["marker"] for ln in result_lines2] == [f"m{i}" for i in range(50, 60)]


def test_naive_timestamp_line_skipped_not_crashed(tmp_path):
    """Fix wave 1, finding 2 (IMPORTANT): a line with a syntactically-valid
    but NAIVE (no Z/offset) ISO timestamp must be SKIPPED during the
    backward scan, not crash the ``ts < since_ts`` comparison against a
    tz-aware ``since_ts`` (``TypeError: can't compare offset-naive and
    offset-aware datetimes``). A torn/partial final line is exactly the
    truncation-safety case this task is about -- one bad line must not take
    down the whole read."""
    path = tmp_path / "session.jsonl"
    base = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
    raw_lines = [
        json.dumps({"timestamp": _iso(base), "marker": "m0"}),
        json.dumps({"timestamp": "2026-07-15T10:01:00", "marker": "naive"}),  # no Z/offset
        json.dumps({"timestamp": _iso(base + timedelta(minutes=2)), "marker": "m2"}),
    ]
    path.write_text("\n".join(raw_lines) + "\n")

    since_ts = base - timedelta(minutes=5)

    result_lines, new_offset, reset = cus._reverse_tail_since(
        path, since_ts, start_offset=None, byte_cap=1024 * 1024)

    assert reset is False
    assert new_offset == path.stat().st_size
    markers = [ln["marker"] for ln in result_lines]
    assert markers == ["m0", "m2"]  # naive line skipped; valid lines intact; no crash


# ---------------------------------------------------------------------------
# _detect_truncation / _reverse_tail_since truncation fallback
# ---------------------------------------------------------------------------

def test_truncation_reset(tmp_path):
    """recorded_offset > current_size => truncation: _detect_truncation
    flags it, and _reverse_tail_since falls back to a fresh bounded
    cold-start read instead of seeking past EOF."""
    path = tmp_path / "session.jsonl"
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

    _write_jsonl(path, [{"timestamp": _iso(now), "marker": "old-1"},
                         {"timestamp": _iso(now), "marker": "old-2"}])
    stale_recorded_offset = path.stat().st_size + 1024  # from before rotation/truncation

    # Simulate rotation: replace with fresh, smaller content.
    _write_jsonl(path, [{"timestamp": _iso(now), "marker": "new-1"}])
    current_size = path.stat().st_size

    assert cus._detect_truncation(stale_recorded_offset, current_size) is True
    assert cus._detect_truncation(current_size, current_size) is False
    assert cus._detect_truncation(current_size - 1, current_size) is False

    since_ts = now - timedelta(minutes=30)
    result_lines, new_offset, reset = cus._reverse_tail_since(
        path, since_ts, start_offset=stale_recorded_offset,
        byte_cap=cus.PER_SESSION_TAIL_BYTES)

    assert reset is True
    assert new_offset == current_size
    assert [ln["marker"] for ln in result_lines] == ["new-1"]


# ---------------------------------------------------------------------------
# _read_active_tails: G0-analogous read-only offsets snapshot
# ---------------------------------------------------------------------------

def test_snapshot_never_writes_offsets(tmp_path, monkeypatch):
    """persist=False (on-demand --json/CLI, §3) leaves the caller's offsets
    registry byte-identical; persist=True (daemon only) advances it."""
    monkeypatch.setattr(cus, "CLAUDE_DIR", tmp_path)
    projects_dir = tmp_path / "projects"
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    path = projects_dir / "slugA" / "session1.jsonl"
    _write_jsonl(path, [{"timestamp": _iso(now), "marker": "a"}])
    _set_mtime(path, now)

    # Empty registry stays empty under persist=False.
    offsets_empty: dict = {}
    result = cus._read_active_tails(now, PRESSURE_CFG, offsets_empty, persist=False)
    assert offsets_empty == {}
    assert result and result[path]  # data still returned to the caller

    # A pre-populated registry is left byte-identical too, not just "empty stays empty".
    pre_populated = {path: 0}
    frozen = dict(pre_populated)
    cus._read_active_tails(now, PRESSURE_CFG, pre_populated, persist=False)
    assert pre_populated == frozen

    # persist=True advances the registry to the file's current EOF offset.
    offsets_live: dict = {}
    cus._read_active_tails(now, PRESSURE_CFG, offsets_live, persist=True)
    assert offsets_live.get(path) == path.stat().st_size


# ---------------------------------------------------------------------------
# _read_active_tails: aggregate PER_CYCLE_TAIL_BYTES cap
# ---------------------------------------------------------------------------

def test_per_cycle_byte_cap(tmp_path, monkeypatch):
    """Once the aggregate tail_bytes_per_cycle budget is exhausted, later
    (budget-order) files stop consuming it -- but they still key into the
    result (low-confidence, empty read) rather than being dropped."""
    monkeypatch.setattr(cus, "CLAUDE_DIR", tmp_path)
    projects_dir = tmp_path / "projects"
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

    p1 = projects_dir / "slugA" / "session1.jsonl"
    p2 = projects_dir / "slugB" / "session2.jsonl"
    p3 = projects_dir / "slugC" / "session3.jsonl"
    for p in (p1, p2, p3):
        _write_jsonl(p, [{"timestamp": _iso(now), "marker": "x"}])
        _set_mtime(p, now)

    per_cycle_cap = p1.stat().st_size  # exactly enough for the first file, no more
    cfg = {"pressure": {"rate_window_min": 10, "tail_bytes_per_cycle": per_cycle_cap}}

    result = cus._read_active_tails(now, cfg, {}, persist=False)

    assert set(result) == {p1, p2, p3}  # nothing dropped from the mapping
    assert result[p1] != []             # first (budget-order) file got its data
    assert result[p2] == []             # budget exhausted -> empty, but still a key
    assert result[p3] == []


def test_per_cycle_byte_cap_persist_preserves_unread_offset(tmp_path, monkeypatch):
    """Fix wave 1, finding 1 (CRITICAL): persist=True + aggregate budget
    exhaustion must NOT advance a starved file's offsets[] entry to its new
    EOF -- that would silently and permanently skip whatever wasn't
    actually read this cycle. The starved file's offset stays exactly as
    it was; a later call with a restored budget then reads everything that
    was pending, nothing lost."""
    monkeypatch.setattr(cus, "CLAUDE_DIR", tmp_path)
    projects_dir = tmp_path / "projects"
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

    p1 = projects_dir / "slugA" / "session1.jsonl"
    p2 = projects_dir / "slugB" / "session2.jsonl"
    for p in (p1, p2):
        _write_jsonl(p, [{"timestamp": _iso(now), "marker": "x"}])
        _set_mtime(p, now)

    ample_cfg = {"pressure": {"rate_window_min": 10, "tail_bytes_per_cycle": 64 * 1024 * 1024}}
    offsets: dict = {}

    # Call 1: ample budget -- both files fully read, offsets recorded for real.
    result1 = cus._read_active_tails(now, ample_cfg, offsets, persist=True)
    assert [ln["marker"] for ln in result1[p2]] == ["x"]
    offset_p2_after_call1 = offsets[p2]
    assert offset_p2_after_call1 == p2.stat().st_size

    # Between cycles, p2 gets new data appended (steady-state growth).
    with p2.open("a") as fh:
        fh.write(json.dumps({"timestamp": _iso(now), "marker": "y"}) + "\n")
    _set_mtime(p2, now)

    # Call 2: per-cycle budget sized to exactly exhaust on p1 (budget-order
    # first, slugA < slugB), leaving p2 fully starved (byte_cap=0) for its
    # steady-state resume -- it can't catch up to the newly appended line
    # this cycle.
    starved_cfg = {"pressure": {"rate_window_min": 10,
                                 "tail_bytes_per_cycle": p1.stat().st_size}}
    result2 = cus._read_active_tails(now, starved_cfg, offsets, persist=True)
    assert result2[p2] == []  # low-confidence: nothing read, still keyed (not dropped)
    assert offsets[p2] == offset_p2_after_call1  # UNCHANGED -- not advanced past what was read

    # Call 3: budget restored -- p2's steady-state resume picks up from the
    # UNCHANGED offset and reads the pending appended line. Nothing skipped.
    result3 = cus._read_active_tails(now, ample_cfg, offsets, persist=True)
    assert [ln["marker"] for ln in result3[p2]] == ["y"]
    assert offsets[p2] == p2.stat().st_size


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
