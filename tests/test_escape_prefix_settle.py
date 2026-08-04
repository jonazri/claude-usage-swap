r"""Escape-prefix coalescing: the payload must never be absorbed as Alt+<byte>.

2026-08-04 field report: the reactive resume landed in the pane as
"he quota-limited account was swapped automatically. …" — leading "T" missing
(recorded that way in the session JSONL, `promptSource: "typed"`). Root cause:
`_resume_pane` sent `Escape Escape` and then the literal message with NO gap,
so the pty byte stream was `\x1b\x1b T h e …`. A terminal keypress parser reads
a lone ESC followed by another byte within its escape-code timeout (node
readline's `escapeCodeTimeout`, 500ms, which Claude Code's Ink TUI inherits) as
Alt+<byte>: the "T" was consumed into one meta-t keypress and never reached the
input box. Measured against a node-readline keypress logger in a real tmux pane:

    gap 0.00s -> {"seq":"\x1b\x1bT","name":"t","meta":true}          # T lost
    gap 0.15s -> {"seq":"\x1b\x1bT","name":"t","meta":true}          # T lost
    gap 0.30s -> {"seq":"\x1b\x1bT","name":"t","meta":true}          # T lost
    gap 0.60s -> {"seq":"\x1b\x1b","name":"escape"} + {"seq":"T",…}   # T lands

The same mechanism eats the draft-submit Enter in `tmux_exit_claude`
(`\x1b\x1b\r` -> one meta+return keypress) — which is the 2026-05-20 GH #11
symptom that function's docstring blames on C-u/multi-line semantics: with the
submit Enter swallowed, `/exit` is appended to the leftover draft and submitted
as a chat message.

That same measurement also shows `Escape Escape` in ONE send-keys call arrives
as a SINGLE meta-escape keypress, so GH #24's "two Escapes to cover nested UI
state" was only ever delivering one. Escapes must therefore be sent as separate
keys, each followed by the settle wait.

These tests pin the invariant at the send layer: between the last Escape of a
safety prefix and whatever is sent next, the code waits longer than the parser's
escape-code timeout. No real tmux is touched and `time.sleep` is faked, so the
assertions are on the recorded call ordering, not on wall clock.

    python3 -m pytest tests/test_escape_prefix_settle.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _recorder(monkeypatch):
    """Record an ordered log of ("keys", keys) / ("text", msg) / ("sleep", secs).

    time.sleep is faked (tests must not burn the real settle waits) but every
    sleep is recorded, which is what lets the gap assertions below measure the
    delay the code WOULD have taken between two sends.
    """
    events: list[tuple] = []
    monkeypatch.setattr(cus, "tmux_is_available", lambda: True)
    monkeypatch.setattr(cus, "tmux_send_keys",
                        lambda pane, *keys, tmux_socket=None: events.append(("keys", keys)) or True)
    monkeypatch.setattr(cus, "tmux_send_text",
                        lambda pane, text, **k: events.append(("text", text)) or True)
    monkeypatch.setattr(cus.time, "sleep", lambda s: events.append(("sleep", s)))
    return events


def _sends(events):
    """Just the deliveries, sleeps dropped."""
    return [e for e in events if e[0] != "sleep"]


def _gap_after_last_escape(events):
    """Seconds slept between the last Escape delivered and the next delivery."""
    escapes = [i for i, e in enumerate(events) if e[0] == "keys" and "Escape" in e[1]]
    assert escapes, f"no Escape was sent at all: {events}"
    gap = 0.0
    for kind, payload in events[escapes[-1] + 1:]:
        if kind != "sleep":
            break
        gap += payload
    return gap


# --------------------------------------------------------------------------
# the constant itself
# --------------------------------------------------------------------------

def test_escape_settle_exceeds_parser_escape_timeout():
    """The settle wait is only meaningful if it clears the parser's window."""
    assert cus.ESCAPE_SETTLE_SECONDS > cus.ESCAPE_CODE_TIMEOUT_SECONDS
    assert cus.ESCAPE_CODE_TIMEOUT_SECONDS >= 0.5  # node readline default


def test_escape_prefix_sends_separate_escapes_each_settled(monkeypatch):
    """Two Escapes must be two keypresses, each followed by the settle wait —
    `send-keys Escape Escape` would arrive as a single meta-escape."""
    events = _recorder(monkeypatch)
    assert cus.tmux_escape_prefix("%1") is True
    assert _sends(events) == [("keys", ("Escape",)), ("keys", ("Escape",))]
    assert events == [
        ("keys", ("Escape",)), ("sleep", cus.ESCAPE_SETTLE_SECONDS),
        ("keys", ("Escape",)), ("sleep", cus.ESCAPE_SETTLE_SECONDS),
    ]


def test_escape_prefix_reports_send_failure(monkeypatch):
    events = _recorder(monkeypatch)
    monkeypatch.setattr(cus, "tmux_send_keys",
                        lambda pane, *keys, tmux_socket=None: events.append(("keys", keys)) or False)
    assert cus.tmux_escape_prefix("%1") is False
    assert _sends(events) == [("keys", ("Escape",))]  # aborts on the first failure


# --------------------------------------------------------------------------
# the reported bug — reactive resume dropped the message's first character
# --------------------------------------------------------------------------

def test_resume_pane_waits_out_escape_timeout_before_the_message(monkeypatch):
    events = _recorder(monkeypatch)
    msg = "The quota-limited account was swapped automatically."
    assert cus._resume_pane("%1", None, msg) is True
    assert ("text", msg) in events, f"message never sent: {events}"
    gap = _gap_after_last_escape(events)
    assert gap > cus.ESCAPE_CODE_TIMEOUT_SECONDS, (
        f"only {gap}s between the Escape prefix and the message — the leading "
        f"character parses as Alt+<char> and is dropped: {events}")


def test_resume_pane_sends_the_message_after_the_escapes(monkeypatch):
    """Ordering is still prefix-then-payload (the GH #24 safety property)."""
    events = _recorder(monkeypatch)
    assert cus._resume_pane("%1", "/tmp/tmux-a", "continue please") is True
    assert _sends(events) == [
        ("keys", ("Escape",)), ("keys", ("Escape",)), ("text", "continue please"),
    ]


def test_resume_pane_aborts_without_sending_when_escape_fails(monkeypatch):
    """Unchanged behavior: a failed prefix leaves the pane's context intact."""
    events = _recorder(monkeypatch)
    monkeypatch.setattr(cus, "tmux_send_keys", lambda *a, **k: False)
    assert cus._resume_pane("%1", None, "continue please") is False
    assert [e for e in events if e[0] == "text"] == []


# --------------------------------------------------------------------------
# same root cause, second symptom — the swallowed draft-submit Enter
# --------------------------------------------------------------------------

def test_exit_claude_waits_out_escape_timeout_before_submit_enter(monkeypatch):
    events = _recorder(monkeypatch)
    cus.tmux_exit_claude("%1")
    gap = _gap_after_last_escape(events)
    assert gap > cus.ESCAPE_CODE_TIMEOUT_SECONDS, (
        f"only {gap}s between the Escape prefix and the draft-submit Enter — it "
        f"parses as meta+return, so /exit appends to the draft: {events}")
    after = _sends(events)[2:]
    assert after[0] == ("keys", ("Enter",)), f"expected the submit Enter next: {after}"
    assert ("text", "/exit") in after


def test_exit_claude_clear_variant_waits_before_ctrl_u(monkeypatch):
    events = _recorder(monkeypatch)
    cus.tmux_exit_claude("%1", draft_handling="clear")
    gap = _gap_after_last_escape(events)
    assert gap > cus.ESCAPE_CODE_TIMEOUT_SECONDS, (
        f"only {gap}s before C-u — it parses as meta+C-u: {events}")
    after = _sends(events)[2:]
    assert after[0] == ("keys", ("C-u",)), f"expected C-u next: {after}"
    assert ("text", "/exit") in after


if __name__ == "__main__":  # standalone runnable, per CONTRIBUTING.md
    sys.exit(__import__("pytest").main([__file__, "-q"]))
