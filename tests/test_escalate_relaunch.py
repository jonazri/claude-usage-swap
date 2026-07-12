"""Task 3 — Fix #1b: escalation relaunch (stale in-memory OAuth token).

After an in-place credential move + gentle resume, a running Claude keeps the OLD
OAuth access token in memory and can keep burning / re-429ing the capped account
until token expiry. The deterministic recovery the operator validated by hand is
`/exit` then re-running the pane's ORIGINAL launch command (which re-reads the
slot's CURRENT credentials). This suite pins the automation of that recovery, run
ONLY as an escalation when the gentle resume demonstrably didn't stick (a second
429 on the same slot within `reactive.escalate_window_seconds`).

Covers brief item 5:
  - last_resume_ts recording (threaded state, backward-compatible default None)
  - window logic (inside -> escalate; outside -> none; repeat-within -> skip+SOS)
  - cmdline recovery unit (fake /proc tree, monkeypatched proc root)
  - safety-pattern rejection -> fallback to resume-message + SOS note
  - /exit-wait-relaunch sequencing (mocked tmux; assert ordering)

No real tmux/proc is touched: every tmux helper is monkeypatched and /proc is a
fake directory tree under tmp_path with cus._PROC_ROOT redirected onto it.

    python3 -m pytest tests/test_escalate_relaunch.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _sess(pane, tmux_socket=None, session_id="sess-1"):
    """A minimal live-session stand-in with the attributes the resume/escalation
    code reads off live_sessions_on_slot()."""
    return type("S", (), {"pane": pane, "tmux_socket": tmux_socket,
                          "session_id": session_id})()


def _iso_secs_ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _config(**reactive):
    r = {"enabled": True, "resume_after_slot_swap": True,
         "resume_message": "continue please",
         "escalate_relaunch": True, "escalate_window_seconds": 900}
    r.update(reactive)
    return {"mode": "per_session", "reactive": r}


def _mkproc(root: Path, pid: int, cmdline: list[str], children: list[int]):
    """Create a fake /proc/<pid> node: cmdline (NUL-separated argv) + the main
    thread's children file at /proc/<pid>/task/<pid>/children."""
    d = root / str(pid)
    (d / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
    (d / "cmdline").write_bytes(b"\x00".join(a.encode() for a in cmdline) + b"\x00")
    (d / "task" / str(pid) / "children").write_text(" ".join(str(c) for c in children))


# --------------------------------------------------------------------------
# config defaults (item 4)
# --------------------------------------------------------------------------

def test_config_defaults_present():
    r = cus.DEFAULT_CONFIG["reactive"]
    assert r["escalate_relaunch"] is True
    assert r["escalate_window_seconds"] == 900


# --------------------------------------------------------------------------
# item 1 — last_resume_ts recording (threaded state, backward-compatible)
# --------------------------------------------------------------------------

def test_resume_records_last_resume_ts_per_slot(monkeypatch):
    monkeypatch.setattr(cus, "live_sessions_on_slot", lambda s: [_sess("%1", "/tmp/tmux-a")])
    monkeypatch.setattr(cus, "tmux_send_keys", lambda *a, **k: True)
    monkeypatch.setattr(cus, "tmux_send_text", lambda *a, **k: True)
    state = {"slots": {"slot-2": {"account": "alpha"}}}
    panes = cus._resume_reactive_slot_sessions("slot-2", _config(), state=state)
    assert panes == ["%1"]
    ts = state["slots"]["slot-2"].get("last_resume_ts")
    assert ts, "expected last_resume_ts to be recorded"
    # recorded value must be recent + parseable
    assert cus._within_escalate_window(ts, 900)


def test_resume_backward_compatible_without_state(monkeypatch):
    """External callers (halted-lane sweep) call with the old 2-arg signature."""
    monkeypatch.setattr(cus, "live_sessions_on_slot", lambda s: [_sess("%1")])
    monkeypatch.setattr(cus, "tmux_send_keys", lambda *a, **k: True)
    monkeypatch.setattr(cus, "tmux_send_text", lambda *a, **k: True)
    panes = cus._resume_reactive_slot_sessions("slot-1", _config())  # no state kwarg
    assert panes == ["%1"]


def test_resume_does_not_record_when_continuation_fails(monkeypatch):
    monkeypatch.setattr(cus, "live_sessions_on_slot", lambda s: [_sess("%1")])
    monkeypatch.setattr(cus, "tmux_send_keys", lambda *a, **k: True)
    monkeypatch.setattr(cus, "tmux_send_text", lambda *a, **k: False)  # send fails
    state = {"slots": {"slot-1": {}}}
    panes = cus._resume_reactive_slot_sessions("slot-1", _config(), state=state)
    assert panes == []
    assert "last_resume_ts" not in state["slots"]["slot-1"]


# --------------------------------------------------------------------------
# item 2/3 — window / escalation decision logic
# --------------------------------------------------------------------------

def test_decision_none_without_prior_resume():
    state = {"slots": {"slot-1": {}}}
    assert cus._escalation_decision(state, _config(), "slot-1") == "none"


def test_decision_escalate_inside_window():
    state = {"slots": {"slot-1": {"last_resume_ts": _iso_secs_ago(60)}}}
    assert cus._escalation_decision(state, _config(), "slot-1") == "escalate"


def test_decision_none_outside_window():
    state = {"slots": {"slot-1": {"last_resume_ts": _iso_secs_ago(2000)}}}
    assert cus._escalation_decision(state, _config(), "slot-1") == "none"


def test_decision_skip_repeat_when_already_escalated_in_window():
    state = {"slots": {"slot-1": {"last_resume_ts": _iso_secs_ago(120),
                                  "last_escalation_ts": _iso_secs_ago(30)}}}
    assert cus._escalation_decision(state, _config(), "slot-1") == "skip_repeat"


def test_decision_escalate_again_after_prior_escalation_aged_out():
    state = {"slots": {"slot-1": {"last_resume_ts": _iso_secs_ago(120),
                                  "last_escalation_ts": _iso_secs_ago(2000)}}}
    assert cus._escalation_decision(state, _config(), "slot-1") == "escalate"


def test_decision_off_when_gate_disabled():
    state = {"slots": {"slot-1": {"last_resume_ts": _iso_secs_ago(60)}}}
    assert cus._escalation_decision(state, _config(escalate_relaunch=False), "slot-1") == "none"


# --------------------------------------------------------------------------
# item 2a — cmdline recovery from a fake /proc tree
# --------------------------------------------------------------------------

def test_recover_pane_relaunch_cmd(monkeypatch, tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    # 100 = pane shell -> 200 = wrapper (first child, the relaunch cmd)
    #                  -> 300 = node hosting claude (deepest 'claude' descendant)
    _mkproc(proc, 100, ["-bash"], [200])
    _mkproc(proc, 200, ["headroom", "wrap", "claude", "--model", "opus", "--", "--resume", "sess-abc"], [300])
    _mkproc(proc, 300, ["node", "/opt/claude/cli.js", "--resume", "sess-abc"], [])
    monkeypatch.setattr(cus, "_PROC_ROOT", proc)
    monkeypatch.setattr(cus, "_pane_pid", lambda pane, tmux_socket=None: 100)

    relaunch, claude_pid = cus._recover_pane_relaunch_cmd("%1", "/tmp/tmux-a")
    assert relaunch == "headroom wrap claude --model opus -- --resume sess-abc"
    assert claude_pid == 300  # deepest descendant whose cmdline contains 'claude'


def test_recover_returns_none_when_no_children(monkeypatch, tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    _mkproc(proc, 100, ["-bash"], [])
    monkeypatch.setattr(cus, "_PROC_ROOT", proc)
    monkeypatch.setattr(cus, "_pane_pid", lambda pane, tmux_socket=None: 100)
    assert cus._recover_pane_relaunch_cmd("%1") == (None, None)


def test_recover_returns_none_when_no_pane_pid(monkeypatch):
    monkeypatch.setattr(cus, "_pane_pid", lambda pane, tmux_socket=None: None)
    assert cus._recover_pane_relaunch_cmd("%1") == (None, None)


def test_pid_alive_reads_proc_root(monkeypatch, tmp_path):
    proc = tmp_path / "proc"
    (proc / "777").mkdir(parents=True)
    monkeypatch.setattr(cus, "_PROC_ROOT", proc)
    assert cus._pid_alive(777) is True
    assert cus._pid_alive(888) is False


def test_wait_for_pid_exit_returns_when_pid_gone(monkeypatch):
    calls = {"n": 0}

    def fake_alive(pid):
        calls["n"] += 1
        return calls["n"] < 3  # alive twice, then gone

    monkeypatch.setattr(cus, "_pid_alive", fake_alive)
    monkeypatch.setattr(cus.time, "sleep", lambda *_a: None)
    assert cus._wait_for_pid_exit(999, timeout=10, interval=0.1) is True
    assert calls["n"] == 3


def test_wait_for_pid_exit_times_out(monkeypatch):
    monkeypatch.setattr(cus, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cus.time, "sleep", lambda *_a: None)
    assert cus._wait_for_pid_exit(999, timeout=0, interval=0.1) is False


# --------------------------------------------------------------------------
# item 2c — safety-pattern rejection -> fallback to resume + SOS note
# --------------------------------------------------------------------------

def test_escalate_falls_back_when_cmdline_unsafe(monkeypatch):
    sends = []
    monkeypatch.setattr(cus, "live_sessions_on_slot", lambda s: [_sess("%1", "/tmp/tmux-a")])
    # recovered command does NOT contain 'claude' -> must NOT /exit the pane
    monkeypatch.setattr(cus, "_recover_pane_relaunch_cmd", lambda pane, sock=None: ("vim /etc/hosts", None))
    monkeypatch.setattr(cus, "tmux_send_keys", lambda *a, **k: True)
    monkeypatch.setattr(cus, "tmux_send_text", lambda pane, text, **k: sends.append(text) or True)
    exited = []
    monkeypatch.setattr(cus, "_escalate_relaunch_pane", lambda *a, **k: exited.append(a) or True)

    state = {"slots": {"slot-1": {"account": "alpha", "last_resume_ts": _iso_secs_ago(60)}}}
    move = {"slot": "slot-1", "from": "alpha", "to": "beta", "gate": "reactive_429", "_escalate": True}
    cus._reactive_escalate_or_resume(move, state, _config())

    assert exited == [], "must never /exit a pane without a claude relaunch command"
    assert sends == ["continue please"], "expected the gentle resume-message fallback"
    note = state["slots"]["slot-1"].get("escalation_skip_note", "")
    assert "could not recover launch command" in note
    assert "last_escalation_ts" not in state["slots"]["slot-1"]


def test_escalate_no_resume_into_bare_shell_after_exit(monkeypatch):
    """If /exit landed but the relaunch send failed, the pane is at a bare shell —
    the resume-message must NOT be typed into it (it would run as a shell command).
    Only an SOS note is recorded so a human relaunches."""
    resumes = []
    monkeypatch.setattr(cus, "live_sessions_on_slot", lambda s: [_sess("%1", "/tmp/tmux-a")])
    monkeypatch.setattr(cus, "_recover_pane_relaunch_cmd",
                        lambda pane, sock=None: ("headroom wrap claude -- --resume x", 300))
    # /exit landed (exited=True) but relaunch failed (relaunched=False)
    monkeypatch.setattr(cus, "_escalate_relaunch_pane", lambda *a, **k: (False, True))
    monkeypatch.setattr(cus, "_resume_pane", lambda *a, **k: resumes.append(a) or True)
    state = {"slots": {"slot-1": {"account": "alpha", "last_resume_ts": _iso_secs_ago(60)}}}
    move = {"slot": "slot-1", "from": "alpha", "to": "beta", "gate": "reactive_429", "_escalate": True}
    cus._reactive_escalate_or_resume(move, state, _config())
    assert resumes == [], "must not type a resume-message into a bare shell after /exit"
    note = state["slots"]["slot-1"].get("escalation_skip_note", "")
    assert "needs manual relaunch" in note
    assert "last_escalation_ts" not in state["slots"]["slot-1"]


def test_escalate_falls_back_to_resume_when_exit_send_fails(monkeypatch):
    """If /exit itself could not be sent, the pane is untouched, so the gentle
    resume-message IS the correct non-destructive fallback."""
    resumes = []
    monkeypatch.setattr(cus, "live_sessions_on_slot", lambda s: [_sess("%1", "/tmp/tmux-a")])
    monkeypatch.setattr(cus, "_recover_pane_relaunch_cmd",
                        lambda pane, sock=None: ("headroom wrap claude -- --resume x", 300))
    monkeypatch.setattr(cus, "_escalate_relaunch_pane", lambda *a, **k: (False, False))
    monkeypatch.setattr(cus, "_resume_pane", lambda *a, **k: resumes.append(a) or True)
    state = {"slots": {"slot-1": {"account": "alpha", "last_resume_ts": _iso_secs_ago(60)}}}
    move = {"slot": "slot-1", "from": "alpha", "to": "beta", "gate": "reactive_429", "_escalate": True}
    cus._reactive_escalate_or_resume(move, state, _config())
    assert len(resumes) == 1, "expected the gentle resume fallback when /exit never landed"
    assert "/exit send failed" in state["slots"]["slot-1"].get("escalation_skip_note", "")


def test_escalate_records_last_escalation_ts_on_success(monkeypatch):
    monkeypatch.setattr(cus, "live_sessions_on_slot", lambda s: [_sess("%1", "/tmp/tmux-a")])
    monkeypatch.setattr(cus, "_recover_pane_relaunch_cmd",
                        lambda pane, sock=None: ("headroom wrap claude -- --resume x", 300))
    monkeypatch.setattr(cus, "_escalate_relaunch_pane", lambda *a, **k: (True, True))
    state = {"slots": {"slot-1": {"account": "alpha", "last_resume_ts": _iso_secs_ago(60)}}}
    move = {"slot": "slot-1", "from": "alpha", "to": "beta", "gate": "reactive_429", "_escalate": True}
    cus._reactive_escalate_or_resume(move, state, _config())
    assert cus._within_escalate_window(state["slots"]["slot-1"]["last_escalation_ts"], 900)


# --------------------------------------------------------------------------
# item 2b — /exit -> wait -> relaunch sequencing (through _execute_slot_moves)
# --------------------------------------------------------------------------

def test_escalation_sequencing_move_exit_wait_relaunch(monkeypatch):
    events = []

    def fake_execute_swap(*a, **k):
        events.append("move")
        return {}

    def fake_send_text(pane, text, **k):
        events.append(("send", text))
        return True

    def fake_wait(pid, **k):
        events.append(("wait", pid))
        return True

    monkeypatch.setattr(cus, "execute_swap", fake_execute_swap)
    monkeypatch.setattr(cus, "live_sessions_on_slot", lambda s: [_sess("%9", "/tmp/tmux-z")])
    monkeypatch.setattr(cus, "_recover_pane_relaunch_cmd",
                        lambda pane, sock=None: ("headroom wrap claude -- --resume abc", 4242))
    monkeypatch.setattr(cus, "tmux_send_text", fake_send_text)
    monkeypatch.setattr(cus, "_wait_for_pid_exit", fake_wait)
    monkeypatch.setattr(cus, "_persist_slot_reactive_state", lambda *a, **k: None)
    monkeypatch.setattr(cus, "_log_decision", lambda *a, **k: None)
    monkeypatch.setattr(cus, "_build_decision_record", lambda *a, **k: {})

    state = {"slots": {"slot-1": {"account": "alpha", "last_resume_ts": _iso_secs_ago(60)}},
             "accounts": {"alpha": {}, "beta": {}}}
    move = {"slot": "slot-1", "from": "alpha", "to": "beta", "gate": "reactive_429",
            "tier": 3, "deferrable": False, "reason": "2nd 429", "_escalate": True}

    cus._execute_slot_moves([move], state, _config(), no_execute=False)

    assert events == [
        "move",
        ("send", "/exit"),
        ("wait", 4242),
        ("send", "headroom wrap claude -- --resume abc"),
    ], f"unexpected sequence: {events}"


def test_non_escalated_reactive_move_uses_plain_resume(monkeypatch):
    """A first 429 (no recent resume) must move + gentle-resume, never /exit."""
    events = []
    monkeypatch.setattr(cus, "execute_swap", lambda *a, **k: events.append("move") or {})
    monkeypatch.setattr(cus, "live_sessions_on_slot", lambda s: [_sess("%1", "/tmp/tmux-a")])
    monkeypatch.setattr(cus, "tmux_send_keys", lambda *a, **k: True)
    monkeypatch.setattr(cus, "tmux_send_text", lambda pane, text, **k: events.append(("send", text)) or True)
    monkeypatch.setattr(cus, "_escalate_relaunch_pane", lambda *a, **k: events.append("EXIT") or True)
    monkeypatch.setattr(cus, "_persist_slot_reactive_state", lambda *a, **k: None)
    monkeypatch.setattr(cus, "_log_decision", lambda *a, **k: None)
    monkeypatch.setattr(cus, "_build_decision_record", lambda *a, **k: {})

    state = {"slots": {"slot-1": {"account": "alpha"}}, "accounts": {"alpha": {}, "beta": {}}}
    move = {"slot": "slot-1", "from": "alpha", "to": "beta", "gate": "reactive_429",
            "tier": 3, "deferrable": False, "reason": "1st 429"}
    cus._execute_slot_moves([move], state, _config(), no_execute=False)

    assert "EXIT" not in events
    assert events[0] == "move"
    assert ("send", "continue please") in events


# --------------------------------------------------------------------------
# item 2b — the /exit-wait-relaunch primitive in isolation
# --------------------------------------------------------------------------

def test_escalate_relaunch_pane_sequence(monkeypatch):
    events = []
    monkeypatch.setattr(cus, "tmux_send_text", lambda pane, text, **k: events.append(("send", text)) or True)
    monkeypatch.setattr(cus, "_wait_for_pid_exit", lambda pid, **k: events.append(("wait", pid)) or True)
    relaunched, exited = cus._escalate_relaunch_pane(
        "%1", "/tmp/tmux-a", "headroom wrap claude -- --resume z", 55, _config())
    assert (relaunched, exited) == (True, True)
    assert events == [("send", "/exit"), ("wait", 55), ("send", "headroom wrap claude -- --resume z")]


def test_escalate_relaunch_pane_aborts_if_exit_send_fails(monkeypatch):
    events = []

    def fake_send(pane, text, **k):
        events.append(text)
        return False  # /exit send fails -> abort, never relaunch

    monkeypatch.setattr(cus, "tmux_send_text", fake_send)
    monkeypatch.setattr(cus, "_wait_for_pid_exit", lambda *a, **k: events.append("wait") or True)
    relaunched, exited = cus._escalate_relaunch_pane("%1", None, "headroom wrap claude", 55, _config())
    assert (relaunched, exited) == (False, False)  # /exit failed -> pane untouched
    assert events == ["/exit"]  # no wait, no relaunch


# --------------------------------------------------------------------------
# SOS surfacing of a skipped escalation
# --------------------------------------------------------------------------

def test_diagnose_surfaces_recent_escalation_skip():
    state = {"slots": {"slot-1": {
        "escalation_skip_note": "escalation skipped: could not recover launch command for slot-1",
        "escalation_skip_ts": _iso_secs_ago(30)}}}
    conds = cus._diagnose_escalation_skips(state, _config())
    assert len(conds) == 1
    assert "slot-1" in conds[0].summary


def test_diagnose_ignores_stale_escalation_skip():
    state = {"slots": {"slot-1": {
        "escalation_skip_note": "escalation skipped: could not recover launch command for slot-1",
        "escalation_skip_ts": _iso_secs_ago(5000)}}}
    assert cus._diagnose_escalation_skips(state, _config()) == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
