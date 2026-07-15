"""Tests for the halted-lane sweep (fix #1a, 2026-07-10 halt incident).

A session can sit parked at Claude Code's rate-limit modal with NO fresh 429
event pending: the daemon was down when the halt fired, the event expired from
the pending queue, or the halt came from a path the hooks never saw. The
per-cycle sweep reads each live slotted session's pane tail once for the modal's
halt signatures and, on a match, either SYNTHESIZES a reactive entry (so the
existing reactive machinery does the credential move + resume next cycle) when
the account is still at/over its leave line, or just nudges the pane when its
account has since reset. The sweep NEVER moves credentials itself.

All tmux access is mocked; nothing here shells out to real tmux.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cus  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_rate_limit_log(monkeypatch, tmp_path):
    """Point RATE_LIMIT_LOG at an empty temp path so _claim_rate_limit_entries
    never picks up the real on-disk 429.log during the store/claim round-trip."""
    monkeypatch.setattr(cus, "RATE_LIMIT_LOG", tmp_path / "429.log")


class _FakeSession:
    """Stand-in for cus.LiveSession — only the attrs the sweep reads."""

    def __init__(self, session_id, pane, tmux_socket=None, account=None):
        self.session_id = session_id
        self.pane = pane
        self.tmux_socket = tmux_socket
        self.account = account


def _cfg(**reactive_overrides) -> dict:
    """A full DEFAULT_CONFIG-based config (capacity gate OFF by default), with
    the reactive block overridden per-test. deepcopy, NOT deep_merge: a
    deep_merge against an empty override aliases DEFAULT_CONFIG's nested dicts,
    so mutating them here would corrupt the module-global default for the rest
    of the pytest session."""
    cfg = copy.deepcopy(cus.DEFAULT_CONFIG)
    cfg["reactive"].update(reactive_overrides)
    return cfg


def _state(pct_5h: float = 95.0, next_swap_at: int = 90) -> dict:
    """State with one live slot on an account whose 5h usage is `pct_5h` and
    whose next ladder step is `next_swap_at`. 95% >= step 90% => over the leave
    line; drop pct_5h below the step to model an account that has since reset."""
    return {
        "accounts": {
            "acctA": {
                "current_5h_pct": pct_5h,
                "current_7d_pct": 5.0,
                "next_swap_at_pct": next_swap_at,
            },
        },
        "slots": {"slot-1": {"account": "acctA"}},
    }


def _install_tmux(monkeypatch, panes_by_slot, available=True):
    """Wire up mocked tmux: `live_sessions_on_slot`, a capture helper that
    returns per-pane text and records every call, and a no-op resume that
    records the slots it was asked to nudge. Returns (capture_calls, resume_calls).

    `panes_by_slot` maps slot name -> list of (_FakeSession, pane_text)."""
    capture_calls: list[tuple] = []
    resume_calls: list[str] = []

    sessions_by_slot = {slot: [s for s, _ in items] for slot, items in panes_by_slot.items()}
    text_by_pane = {
        (s.tmux_socket, s.pane): text
        for items in panes_by_slot.values()
        for s, text in items
    }

    monkeypatch.setattr(cus, "tmux_is_available", lambda: available)
    monkeypatch.setattr(cus, "live_sessions_on_slot", lambda slot: sessions_by_slot.get(slot, []))

    def _fake_capture(pane, tmux_socket=None, lines=30):
        capture_calls.append((pane, tmux_socket, lines))
        return text_by_pane.get((tmux_socket, pane))

    monkeypatch.setattr(cus, "tmux_capture_pane", _fake_capture)
    monkeypatch.setattr(cus, "_resume_reactive_slot_sessions",
                        lambda slot, config: resume_calls.append(slot) or [])
    return capture_calls, resume_calls


HALT_TEXT = "prompt\nStop and wait for limit to reset\n> "


# --------------------------------------------------------------------------
# 1. Signature matching table
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sig", list(cus.HALT_PANE_SIGNATURES))
def test_each_builtin_signature_triggers_synthesis(monkeypatch, sig):
    """Every built-in halt signature, embedded in a pane tail, is detected."""
    state = _state()
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _install_tmux(monkeypatch, {"slot-1": [(sess, f"...\n{sig}\n> ")]})
    out = cus._sweep_halted_lanes(state, cfg)
    assert len(out) == 1
    assert out[0]["slot"] == "slot-1"


def test_config_extra_signature_matches(monkeypatch):
    state = _state()
    cfg = _cfg(halt_signatures_extra=["CUSTOM MODAL LINE"])
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _install_tmux(monkeypatch, {"slot-1": [(sess, "noise\nCUSTOM MODAL LINE\n")]})
    out = cus._sweep_halted_lanes(state, cfg)
    assert len(out) == 1


def test_signature_match_is_case_sensitive(monkeypatch):
    """The list is case-sensitive: a lowercased signature must NOT match."""
    state = _state()
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _install_tmux(monkeypatch, {"slot-1": [(sess, "stop and wait for limit to reset")]})
    out = cus._sweep_halted_lanes(state, cfg)
    assert out == []


def test_no_signature_no_action(monkeypatch):
    """A pane with no halt signature yields no synthesis and no nudge."""
    state = _state()
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _, resume_calls = _install_tmux(
        monkeypatch, {"slot-1": [(sess, "just a normal prompt\n> ")]})
    out = cus._sweep_halted_lanes(state, cfg)
    assert out == []
    assert resume_calls == []
    assert "pending_429_entries" not in state


# --------------------------------------------------------------------------
# 2. Synthesis only when over line + no pending entry
# --------------------------------------------------------------------------

def test_synthesizes_when_over_line_and_no_pending(monkeypatch):
    state = _state(pct_5h=95.0, next_swap_at=90)
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _, resume_calls = _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})
    out = cus._sweep_halted_lanes(state, cfg)
    assert len(out) == 1
    # Injected into the same pending queue the hooks feed.
    assert len(state["pending_429_entries"]) == 1
    # Over-line synthesis moves via the reactive path, NOT a direct nudge.
    assert resume_calls == []


def test_under_line_direct_resume_no_synth(monkeypatch):
    """Signature present but the account already reset (below its leave line):
    skip the move, nudge the pane directly."""
    state = _state(pct_5h=40.0, next_swap_at=90)  # 40% < step 90% => under line
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _, resume_calls = _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})
    out = cus._sweep_halted_lanes(state, cfg)
    assert out == []
    assert resume_calls == ["slot-1"]
    assert "pending_429_entries" not in state


def test_pending_entry_already_covers_slot_skips_synth(monkeypatch):
    """A still-pending reactive entry for the slot suppresses re-synthesis."""
    state = _state()
    state["pending_429_entries"] = [{
        "ts": cus.now_iso(), "session_id": "sess-0", "match": "429",
        "source": "stopfailure", "slot": "slot-1", "account": "acctA",
    }]
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _, resume_calls = _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})
    out = cus._sweep_halted_lanes(state, cfg)
    assert out == []
    assert len(state["pending_429_entries"]) == 1  # unchanged
    assert resume_calls == []


def test_reactive_move_this_cycle_covers_slot_skips_synth(monkeypatch):
    """A reactive move already queued this cycle for the slot suppresses synthesis."""
    state = _state()
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})
    out = cus._sweep_halted_lanes(
        state, cfg, reactive_moves=[{"slot": "slot-1", "to": "acctB"}])
    assert out == []
    assert "pending_429_entries" not in state


# --------------------------------------------------------------------------
# 3. Synthesized entry shape matches hook records
# --------------------------------------------------------------------------

def test_synthesized_entry_shape_matches_hook_records(monkeypatch):
    state = _state()
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%3", "/tmp/tmux-sock", "acctA")
    _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})
    out = cus._sweep_halted_lanes(state, cfg)
    entry = out[0]
    # Same six-field dict shape _read_rate_limit_log_since produces from a hook
    # CSV record; source carries the synthetic-origin token.
    assert set(entry) == {"ts", "session_id", "match", "source", "slot", "account"}
    assert entry["source"] == "halted_lane_sweep"
    assert entry["session_id"] == "sess-1"
    assert entry["slot"] == "slot-1"
    assert entry["account"] == "acctA"
    # ts is a parseable ISO instant.
    from datetime import datetime
    datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))


def test_synthesized_entry_round_trips_store_and_claim(monkeypatch):
    """The stored entry survives the hooks' store/claim path with all fields
    the reactive per_session path indexes (notably e['session_id'])."""
    state = _state()
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})
    cus._sweep_halted_lanes(state, cfg)
    claimed = cus._claim_rate_limit_entries(state)
    assert len(claimed) == 1
    e = claimed[0]
    # These direct-index accesses mirror check_rate_limit_reactive_per_session;
    # a dropped field would KeyError there.
    assert e["session_id"] == "sess-1"
    assert e["slot"] == "slot-1"
    assert e["account"] == "acctA"
    assert e["source"] == "halted_lane_sweep"


def test_sweep_appends_and_preserves_existing_pending(monkeypatch):
    """Append semantics: synthesis must not clobber an unrelated pending entry
    (e.g. a held 429 on another slot)."""
    state = {
        "accounts": {
            "acctA": {"current_5h_pct": 95.0, "current_7d_pct": 5.0, "next_swap_at_pct": 90},
            "acctB": {"current_5h_pct": 95.0, "current_7d_pct": 5.0, "next_swap_at_pct": 90},
        },
        "slots": {"slot-1": {"account": "acctA"}, "slot-2": {"account": "acctB"}},
        "pending_429_entries": [{
            "ts": cus.now_iso(), "session_id": "sess-other", "match": "429",
            "source": "stopfailure", "slot": "slot-2", "account": "acctB",
        }],
    }
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    # Only slot-1 is halted; slot-2's live scan returns nothing.
    _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})
    cus._sweep_halted_lanes(state, cfg)
    slots = sorted(e["slot"] for e in state["pending_429_entries"])
    assert slots == ["slot-1", "slot-2"]


# --------------------------------------------------------------------------
# 4. Cheapness: gate-off / tmux-absent => no captures; one capture per pane
# --------------------------------------------------------------------------

def test_gate_off_takes_no_captures(monkeypatch):
    state = _state()
    cfg = _cfg(halted_lane_sweep=False)
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    capture_calls, resume_calls = _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})
    out = cus._sweep_halted_lanes(state, cfg)
    assert out == []
    assert capture_calls == []
    assert resume_calls == []


def test_tmux_absent_takes_no_captures(monkeypatch):
    state = _state()
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    capture_calls, _ = _install_tmux(
        monkeypatch, {"slot-1": [(sess, HALT_TEXT)]}, available=False)
    out = cus._sweep_halted_lanes(state, cfg)
    assert out == []
    assert capture_calls == []


def test_at_most_one_capture_per_pane(monkeypatch):
    """Two live sessions sharing a pane -> exactly one capture for that pane."""
    state = _state()
    cfg = _cfg()
    s1 = _FakeSession("sess-1", "%1", None, "acctA")
    s2 = _FakeSession("sess-2", "%1", None, "acctA")  # same pane
    capture_calls, _ = _install_tmux(
        monkeypatch, {"slot-1": [(s1, HALT_TEXT), (s2, HALT_TEXT)]})
    cus._sweep_halted_lanes(state, cfg)
    panes = [c[0] for c in capture_calls]
    assert panes.count("%1") == 1


def test_locked_slot_is_never_swept(monkeypatch):
    """A locked slot is user-frozen: no capture, no synthesis, no nudge."""
    state = _state()
    cfg = _cfg()
    cfg["session_locks"]["locked_slots"] = ["slot-1"]
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    capture_calls, resume_calls = _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})
    out = cus._sweep_halted_lanes(state, cfg)
    assert out == []
    assert capture_calls == []
    assert resume_calls == []


# --------------------------------------------------------------------------
# 5. Nudge cooldown + per-slot dedup (review finding 2026-07-12)
#
# The under-line direct-nudge path used to re-fire every cycle for as long as
# a halt signature persisted in the pane (concrete false-positive: this repo's
# own source files contain the halt signatures verbatim). Fix: a per-slot
# cooldown (reactive.nudge_min_interval_seconds, default 900) recorded in
# state["slots"][slot]["last_sweep_nudge_ts"], plus per-SLOT (not per-pane)
# dedup of the _resume_reactive_slot_sessions call.
# --------------------------------------------------------------------------

def test_nudge_recorded_and_skipped_within_window(monkeypatch, capsys):
    """First sweep nudges and stamps last_sweep_nudge_ts; an immediate second
    sweep is within the default 900s cooldown and must NOT nudge again — and
    the skip is logged (debug-level line), not silent."""
    state = _state(pct_5h=40.0, next_swap_at=90)  # under line
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _, resume_calls = _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})

    cus._sweep_halted_lanes(state, cfg)
    assert resume_calls == ["slot-1"]
    assert "last_sweep_nudge_ts" in state["slots"]["slot-1"]
    from datetime import datetime
    datetime.fromisoformat(state["slots"]["slot-1"]["last_sweep_nudge_ts"].replace("Z", "+00:00"))

    capsys.readouterr()  # discard first-sweep output
    out = cus._sweep_halted_lanes(state, cfg)
    assert out == []
    assert resume_calls == ["slot-1"]  # unchanged: no second nudge
    captured = capsys.readouterr()
    assert "nudge" in captured.out.lower()  # skip is logged, not silent


def test_nudge_fires_again_after_window_elapses(monkeypatch):
    """A last_sweep_nudge_ts older than the configured floor no longer holds
    the nudge back."""
    from datetime import datetime, timedelta, timezone
    state = _state(pct_5h=40.0, next_swap_at=90)
    state["slots"]["slot-1"]["last_sweep_nudge_ts"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat()
    cfg = _cfg(nudge_min_interval_seconds=900)
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _, resume_calls = _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})
    cus._sweep_halted_lanes(state, cfg)
    assert resume_calls == ["slot-1"]


def test_two_matching_panes_one_slot_nudged_once(monkeypatch):
    """Two live panes on the same slot both matching a halt signature (the
    cosmetic half of the finding) must produce exactly ONE
    _resume_reactive_slot_sessions call for the slot, not two — it already
    walks every pane on the slot internally."""
    state = _state(pct_5h=40.0, next_swap_at=90)
    cfg = _cfg()
    s1 = _FakeSession("sess-1", "%1", None, "acctA")
    s2 = _FakeSession("sess-2", "%2", None, "acctA")  # different pane, same slot
    _, resume_calls = _install_tmux(
        monkeypatch, {"slot-1": [(s1, HALT_TEXT), (s2, HALT_TEXT)]})
    cus._sweep_halted_lanes(state, cfg)
    assert resume_calls == ["slot-1"]


def test_cooldown_does_not_affect_over_line_synthesis(monkeypatch):
    """The cooldown is scoped to the direct-nudge (under-line) path only —
    a recent last_sweep_nudge_ts must not suppress over-line synthesis."""
    state = _state(pct_5h=95.0, next_swap_at=90)  # over line -> synth path
    state["slots"]["slot-1"]["last_sweep_nudge_ts"] = cus.now_iso()  # "just nudged"
    cfg = _cfg()
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _, resume_calls = _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})
    out = cus._sweep_halted_lanes(state, cfg)
    assert len(out) == 1
    assert resume_calls == []


def test_nudge_min_interval_zero_disables_cooldown(monkeypatch):
    """nudge_min_interval_seconds: 0 restores the pre-2026-07-12 behavior:
    every under-line match nudges, regardless of how recently the slot was
    last nudged."""
    state = _state(pct_5h=40.0, next_swap_at=90)
    state["slots"]["slot-1"]["last_sweep_nudge_ts"] = cus.now_iso()  # just nudged
    cfg = _cfg(nudge_min_interval_seconds=0)
    sess = _FakeSession("sess-1", "%1", None, "acctA")
    _, resume_calls = _install_tmux(monkeypatch, {"slot-1": [(sess, HALT_TEXT)]})
    cus._sweep_halted_lanes(state, cfg)
    assert resume_calls == ["slot-1"]
