"""Task 11 (spec-2 token-pressure forecaster, STAGE 1, Phase D linchpin):
per-message account time-join + launch-time degradation + unattributed
residual + disjoint rotatable/pinned burn partition (§3, FACT #5, G8).

Covers ``_session_account_intervals``, ``_join_usage_account``,
``_attribute_burn``, ``_partition_burn`` -- the four functions Phase D
introduces. Tasks 5/7/9 already consume a partition object via an injected
synthetic ``FakePartition`` (see ``tests/test_pressure_{pool_curve,pinned,
reqreduction,level}.py``); the real ``PartitionedTable`` built here exposes
the SAME public accessors (``pinned_burn_units``/``rotatable_burn_units``)
so Task 20's real wiring drops in unchanged.

HARNESS (reused by every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Add the repo root to ``sys.path`` and ``import cus``. Run with ``python -m
pytest tests/ -q`` (same command CI uses); a single file runs standalone via
``python tests/test_pressure_attribution.py``.
"""

import contextlib
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

# Real-transcript-shaped usage weights (verified fields: input_tokens,
# output_tokens, cache_read_input_tokens, cache_creation_input_tokens).
WEIGHTS = {
    "input_tokens": 1.0,
    "output_tokens": 5.0,
    "cache_read_input_tokens": 0.25,
    "cache_creation_input_tokens": 1.5,
}


def _iso(dt: datetime) -> str:
    """ISO-ms timestamp string matching real transcript/sessions.log rows."""
    return dt.isoformat().replace("+00:00", "Z")


def _row(ts: datetime, session_id: str, account: str, pane: str = "%1",
         tmux_socket: str | None = "/tmp/tmux-1000/default",
         cwd: str = "/home/yaz/project") -> dict:
    """A `_parse_sessions_log()`-shaped row (that function owns the raw CSV
    6-col/5-col parsing; `_session_account_intervals` consumes its output)."""
    return {
        "ts": _iso(ts),
        "session_id": session_id,
        "account": account,
        "pane": pane,
        "tmux_socket": tmux_socket,
        "cwd": cwd,
    }


def _usage(input_tokens=0, output_tokens=0, cache_read=0, cache_create=0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create,
    }


def _burn(usage: dict, weights: dict = WEIGHTS) -> float:
    """Independent recomputation of the expected weighted burn (mirrors
    `_message_burn_units`'s dot product, kept separate so the test doesn't
    just re-assert the implementation)."""
    return sum(usage.get(k, 0) * w for k, w in weights.items())


def _assistant_line(session_id: str, uid: str, ts: datetime, usage: dict) -> dict:
    return {
        "type": "assistant",
        "uuid": uid,
        "sessionId": session_id,
        "timestamp": _iso(ts),
        "message": {"usage": usage, "model": "claude-opus-4-8"},
    }


# ---------------------------------------------------------------------------
# _session_account_intervals / _join_usage_account: time-join + degradation
# ---------------------------------------------------------------------------

def test_timejoin_launch_time():
    """FACT #5: a session with only ONE sessions.log row (no rotation rows,
    today's real shape) yields exactly one interval, and any usage line
    joins it with confidence="launch-time" -- the degraded case."""
    rows = [_row(NOW, "s1", "acctX")]
    intervals = cus._session_account_intervals(rows)
    assert list(intervals) == ["s1"]
    assert len(intervals["s1"]) == 1

    account, confidence = cus._join_usage_account(NOW + timedelta(minutes=5),
                                                    intervals["s1"])
    assert account == "acctX"
    assert confidence == "launch-time"


def test_rotation_rows_would_join_midwindow():
    """Synthetic rotation rows (a second sessions.log row for the SAME
    session_id, a different account) prove the join is TIMESTAMP-DRIVEN,
    not a hardcoded single-account assumption: a usage line mid-window
    (after the rotation) joins the account active AT ITS OWN timestamp,
    not the launch account. Confidence is "joined" (multi-interval), never
    "launch-time" -- today's single-interval degrade is the special case."""
    t_launch = NOW
    t_rotate = NOW + timedelta(minutes=10)
    rows = [
        _row(t_launch, "s1", "acctA"),
        _row(t_rotate, "s1", "acctB"),
    ]
    intervals = cus._session_account_intervals(rows)["s1"]
    assert len(intervals) == 2

    before, conf_before = cus._join_usage_account(t_launch + timedelta(minutes=5),
                                                    intervals)
    after, conf_after = cus._join_usage_account(t_rotate + timedelta(minutes=5),
                                                  intervals)
    assert before == "acctA"
    assert after == "acctB"
    assert conf_before == "joined"
    assert conf_after == "joined"


def test_launch_fallback_is_logged():
    """G8: the launch-time degrade is a KNOWN, LOGGED limitation, never
    silent -- a structured `pressure.attribution.launch_time_fallback` line
    naming the session is emitted every time the single-interval fallback
    fires."""
    rows = [_row(NOW, "sLog", "acctLog")]
    intervals = cus._session_account_intervals(rows)["sLog"]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cus._join_usage_account(NOW + timedelta(minutes=1), intervals)
    out = buf.getvalue()

    assert "pressure.attribution.launch_time_fallback" in out
    assert "session_id=sLog" in out
    assert "account=acctLog" in out


def test_legacy_5col_and_6col_parse():
    """`_session_account_intervals` consumes `_parse_sessions_log()`'s
    ALREADY-NORMALIZED output, which handles both on-disk shapes: 6-col
    `ts,session_id,account,pane,tmux_socket,cwd` and legacy 5-col
    `ts,session_id,account,pane,cwd` (tmux_socket -> None, cwd last so it
    may embed commas, FACT #5)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "sessions.log"
        t6 = NOW
        t5 = NOW + timedelta(minutes=1)
        lines = [
            f"{_iso(t6)},sA,acctA,%1,/tmp/tmux-1000/default,/home/proj,with,commas\n",
            f"{_iso(t5)},sB,acctB,%2,/home/proj2\n",  # legacy 5-col
        ]
        log_path.write_text("".join(lines))

        orig_log = cus.SESSIONS_LOG
        cus.SESSIONS_LOG = log_path
        try:
            rows = cus._parse_sessions_log()
        finally:
            cus.SESSIONS_LOG = orig_log

        intervals = cus._session_account_intervals(rows)

        assert intervals["sA"][0].account == "acctA"
        assert intervals["sA"][0].pane == "%1"
        assert intervals["sA"][0].tmux_socket == "/tmp/tmux-1000/default"
        assert intervals["sA"][0].cwd == "/home/proj,with,commas"

        assert intervals["sB"][0].account == "acctB"
        assert intervals["sB"][0].pane == "%2"
        assert intervals["sB"][0].tmux_socket is None  # 5-col legacy -> None
        assert intervals["sB"][0].cwd == "/home/proj2"


# ---------------------------------------------------------------------------
# _attribute_burn: dedup, sum, unattributed residual
# ---------------------------------------------------------------------------

def test_unattributed_residual_not_rescaled():
    """Burn that fails to time-join (its session has NO sessions.log
    history at all) is kept as an explicit residual -- it is NEVER folded
    back into a joined session's total (which stays exactly the sum of ITS
    OWN lines), and `residual_fraction` reflects the true split."""
    rows = [_row(NOW, "sK", "acctK")]
    intervals = cus._session_account_intervals(rows)

    u1 = _usage(input_tokens=100, output_tokens=10)
    u2 = _usage(cache_read=400, cache_create=20)
    u3 = _usage(input_tokens=9999, output_tokens=9999)  # the orphan's burn

    tails = {
        Path("/tmp/a.jsonl"): [
            _assistant_line("sK", "uidA", NOW + timedelta(minutes=1), u1),
            _assistant_line("sK", "uidB", NOW + timedelta(minutes=2), u2),
            # sUnknown has ZERO sessions.log rows -> intervals.get() == [].
            _assistant_line("sUnknown", "uidC", NOW + timedelta(minutes=1), u3),
        ],
    }

    table = cus._attribute_burn(tails, intervals, WEIGHTS)

    expected_known = _burn(u1) + _burn(u2)
    expected_residual = _burn(u3)

    assert table.burn_for("acctK", "sK") == pytest.approx(expected_known)
    assert table.residual_for() == pytest.approx(expected_residual)
    assert table.residual_fraction == pytest.approx(
        expected_residual / (expected_known + expected_residual))


def test_dedup_window_7d():
    """The SAME (session_id, uuid) message re-appearing (e.g. re-read by an
    overlapping tail window, possibly from a different path/file) is
    counted exactly ONCE, even when the two occurrences are DAYS apart --
    proving the dedup set is not narrowly scoped to a single short read
    cycle but spans the wide (7d, §10.9) attribution window."""
    rows = [_row(NOW - timedelta(days=4), "sD", "acctD")]
    intervals = cus._session_account_intervals(rows)

    u = _usage(input_tokens=200, output_tokens=40)
    t_first = NOW - timedelta(days=3)
    t_dup = t_first + timedelta(days=3)  # 3 days later, still within 7d

    tails = {
        Path("/tmp/first.jsonl"): [
            _assistant_line("sD", "same-uuid", t_first, u),
        ],
        Path("/tmp/dup.jsonl"): [
            _assistant_line("sD", "same-uuid", t_dup, u),
        ],
    }

    table = cus._attribute_burn(tails, intervals, WEIGHTS)
    assert table.burn_for("acctD", "sD") == pytest.approx(_burn(u))  # once, not twice


# ---------------------------------------------------------------------------
# _partition_burn: disjoint rotatable/pinned split (the linchpin)
# ---------------------------------------------------------------------------

def test_pinned_session_all_pinned():
    """A session on a LOCKED slot (frozen account -- ladder/hard-cap/
    reactive-429/idle-gc all skip it) is entirely PINNED: 0 rotatable."""
    table = cus.AttributionTable()
    table.per_session[("acctLocked", "sLocked")] = 12.0
    table.session_pane["sLocked"] = "%1"

    state = {"slots": {"slot-2": {"account": "acctLocked"}}}
    config = {"session_locks": {"pinned": {}, "locked_slots": ["slot-2"]}}

    orig = cus.session_current_slot
    cus.session_current_slot = lambda sid: {"sLocked": "slot-2"}.get(sid)
    try:
        part = cus._partition_burn(table, state, config)
    finally:
        cus.session_current_slot = orig

    assert part.pinned_burn_units("acctLocked", "5h") == pytest.approx(12.0)
    assert part.rotatable_burn_units("acctLocked", "5h") == 0.0
    assert part.pinned_burn_units("acctLocked", "7d") == pytest.approx(12.0)


def test_rotatable_session_all_pool():
    """A session on an UNLOCKED live slot is entirely ROTATABLE: 0 pinned,
    credited to the account CURRENTLY backing that slot."""
    table = cus.AttributionTable()
    table.per_session[("acctStale", "sRot")] = 9.0
    table.session_pane["sRot"] = "%2"

    state = {"slots": {"slot-1": {"account": "acctFresh"}}}
    config = {"session_locks": {"pinned": {}, "locked_slots": []}}

    orig = cus.session_current_slot
    cus.session_current_slot = lambda sid: {"sRot": "slot-1"}.get(sid)
    try:
        part = cus._partition_burn(table, state, config)
    finally:
        cus.session_current_slot = orig

    assert part.rotatable_burn_units("acctFresh", "5h") == pytest.approx(9.0)
    assert part.pinned_burn_units("acctFresh", "5h") == 0.0
    # The stale attributed-account name never appears as a pool key.
    assert part.rotatable_burn_units("acctStale", "5h") == 0.0
    assert part.pinned_burn_units("acctStale", "5h") == 0.0


def test_burn_partition_disjoint_sums_to_total():
    """Across a mix of locked, rotatable, AND bare (no-slot, Gap A)
    sessions, each session's rotatable + pinned components are DISJOINT
    (never both) and sum EXACTLY to its total attributed burn -- the
    invariant every downstream trigger (Tasks 5/7) depends on."""
    table = cus.AttributionTable()
    table.per_session[("acctLocked", "sLocked")] = 10.0
    table.per_session[("acctOld", "sRot")] = 7.0
    table.per_session[("acctBare", "sBare")] = 3.0
    table.session_pane["sLocked"] = "%1"
    table.session_pane["sRot"] = "%2"
    table.session_pane["sBare"] = "%3"

    state = {"slots": {
        "slot-1": {"account": "acctRot"},   # current backer != attributed acctOld
        "slot-2": {"account": "acctLocked"},
    }}
    config = {
        "session_locks": {"pinned": {"%3": "acctBare"}, "locked_slots": ["slot-2"]},
    }

    orig = cus.session_current_slot
    cus.session_current_slot = lambda sid: {
        "sLocked": "slot-2", "sRot": "slot-1", "sBare": None,
    }.get(sid)
    try:
        part = cus._partition_burn(table, state, config)
    finally:
        cus.session_current_slot = orig

    # Disjoint + sums-to-total per session (checked via the internal detail
    # map every real PartitionedTable exposes alongside the FakePartition-
    # compatible accessors).
    for (account, session_id), total in table.per_session.items():
        p, r = part.per_session[(account, session_id)]
        assert p == 0.0 or r == 0.0, f"{session_id} split across both buckets"
        assert p + r == pytest.approx(total)

    # Locked -> pinned, credited to its own (frozen) account.
    assert part.pinned_burn_units("acctLocked", "5h") == pytest.approx(10.0)
    assert part.rotatable_burn_units("acctLocked", "5h") == 0.0

    # Rotatable -> credited to the slot's CURRENT backing account, not the
    # (possibly stale) attributed account (Gap B).
    assert part.rotatable_burn_units("acctRot", "5h") == pytest.approx(7.0)
    assert part.pinned_burn_units("acctOld", "5h") == 0.0
    assert part.rotatable_burn_units("acctOld", "5h") == 0.0

    # Bare (no slot) -> pinned to its own account regardless of the
    # explicit pinned-map entry (Gap A: a non-lane session can never
    # rotate through the pool either way).
    assert part.pinned_burn_units("acctBare", "5h") == pytest.approx(3.0)
    assert part.rotatable_burn_units("acctBare", "5h") == 0.0

    # Global disjoint-sums-to-total: every unit of burn landed in EXACTLY
    # one bucket, none dropped, none duplicated.
    total_in = sum(table.per_session.values())
    total_out = sum(part._pinned.values()) + sum(part._rotatable.values())
    assert total_out == pytest.approx(total_in)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
