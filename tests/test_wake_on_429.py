"""Tests for wake-on-429 (fix #2, 2026-07-10 halt incident).

Before this fix, a 429 hook record just sat in 429.log until the next
poll_interval_seconds cycle picked it up — with a 300s interval and Claude
Code's session-limit modal giving up in ~1-2 min, the reactive swap routinely
landed AFTER the halt. This pins two halves of the fix:

  Daemon side (`cus._interruptible_sleep`) — the main loop's inter-cycle sleep
  now happens in <=5s slices; between slices it checks for
  `$ACCOUNTS_DIR/wake-429` and, if present, consumes it (deletes + logs) and
  returns immediately instead of sleeping out the rest of the interval. Gated
  by `reactive.wake_on_429` (default True); gate-off is a single uninterrupted
  time.sleep(), bit-for-bit as before this fix.

  Hook side (cus_post_tool_use_failure.sh / cus_stop_failure.sh) — both
  scripts now `touch $ACCOUNTS_DIR/wake-429` right after appending a detected
  429 to 429.log. That touch is UNCONDITIONAL (a hook script can't read
  config.yaml, so it can't itself check reactive.wake_on_429) — it only
  happens on the same code path that already logs a real detection, never on
  the early `exit 0` paths for non-matching events.

These shell out to the real hook scripts (extending tests/test_429_hooks.py's
convention) and drive `_interruptible_sleep` directly with monkeypatched
time.sleep/time.monotonic + a tmp ACCOUNTS_DIR, per the brief.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cus  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
STOP_FAILURE = HOOKS / "cus_stop_failure.sh"
PTUF = HOOKS / "cus_post_tool_use_failure.sh"


def _cfg(wake_on_429: bool = True) -> dict:
    return {"reactive": {"wake_on_429": wake_on_429}}


# --------------------------------------------------------------------------
# Daemon side: _interruptible_sleep
# --------------------------------------------------------------------------

def test_no_wake_sleeps_the_full_interval_in_slices(tmp_path, monkeypatch):
    """No wake file ever appears -> full duration slept, in <=5s slices, None returned."""
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(cus.time, "sleep", lambda s: calls.append(s))

    result = cus._interruptible_sleep(12, _cfg())

    assert result is None
    assert calls == [5.0, 5.0, 2.0]
    assert sum(calls) == 12


def test_gate_off_is_a_single_uninterrupted_sleep(tmp_path, monkeypatch):
    """reactive.wake_on_429: false -> byte-identical original behavior: one
    time.sleep() call for the whole duration, wake file never consulted."""
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", tmp_path)
    (tmp_path / "wake-429").touch()  # even if present, gate-off must ignore it
    calls = []
    monkeypatch.setattr(cus.time, "sleep", lambda s: calls.append(s))

    result = cus._interruptible_sleep(37, _cfg(wake_on_429=False))

    assert result is None
    assert calls == [37]
    assert (tmp_path / "wake-429").exists()  # never consumed


def test_wake_file_present_at_wait_start_consumed_immediately(tmp_path, monkeypatch, capsys):
    """An event that arrived during the PREVIOUS cycle's body should shorten
    THIS wait, not be ignored — consumed unconditionally at wait-start."""
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", tmp_path)
    wake = tmp_path / "wake-429"
    wake.touch()
    calls = []
    monkeypatch.setattr(cus.time, "sleep", lambda s: calls.append(s))

    result = cus._interruptible_sleep(20, _cfg())

    assert result == "wake-429"
    assert calls == []  # never slept at all
    assert not wake.exists()  # consumed
    out = capsys.readouterr().out
    assert "wake: 429 hook event" in out
    assert "0s into a 20s sleep" in out


def test_wake_file_appears_mid_sleep_exits_early_and_consumes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", tmp_path)
    wake = tmp_path / "wake-429"
    clock = {"t": 1000.0}
    calls = []

    def fake_sleep(s):
        calls.append(s)
        clock["t"] += s
        if len(calls) == 2:  # hook fires 10s into the wait
            wake.touch()

    monkeypatch.setattr(cus.time, "sleep", fake_sleep)
    monkeypatch.setattr(cus.time, "monotonic", lambda: clock["t"])

    result = cus._interruptible_sleep(20, _cfg())

    assert result == "wake-429"
    assert calls == [5.0, 5.0]  # stopped right after detection, no 3rd/4th slice
    assert not wake.exists()
    out = capsys.readouterr().out
    assert "10s into a 20s sleep" in out


def test_debounce_repeated_touches_in_same_wait_do_not_double_fire(tmp_path, monkeypatch, capsys):
    """The reactive path's own min_seconds_between_reactive_swaps handles
    per-account rate limiting; this only guards that a single wait can't
    resolve as "woken" more than once even if the hook fires repeatedly."""
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", tmp_path)
    wake = tmp_path / "wake-429"
    calls = []

    def fake_sleep(s):
        calls.append(s)
        wake.touch()  # re-touched on every slice, simulating rapid repeat 429s

    monkeypatch.setattr(cus.time, "sleep", fake_sleep)

    result = cus._interruptible_sleep(30, _cfg())

    assert result == "wake-429"
    assert calls == [5.0]  # only the first slice ran before returning
    out = capsys.readouterr().out
    assert out.count("wake: 429 hook event") == 1
    assert not wake.exists()


def test_stale_wake_file_mid_slice_with_old_mtime_is_ignored(tmp_path, monkeypatch):
    """mtime guard: a wake file that shows up mid-wait but carries an mtime
    from BEFORE this wait started (e.g. some leftover/unconsumed file) must
    not be treated as a fresh event — only a touch at/after wait-start counts
    once we're past the wait-start check itself."""
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", tmp_path)
    wake = tmp_path / "wake-429"
    calls = []
    real_time = cus.time.time

    def fake_sleep(s):
        calls.append(s)
        if len(calls) == 1:
            wake.touch()
            old = real_time() - 3600
            os.utime(wake, (old, old))

    monkeypatch.setattr(cus.time, "sleep", fake_sleep)

    result = cus._interruptible_sleep(12, _cfg())

    assert result is None
    assert calls == [5.0, 5.0, 2.0]
    assert wake.exists()  # never consumed — stale relative to wait_start_wall


# --------------------------------------------------------------------------
# Hook side: unconditional touch on detection, no touch otherwise
# --------------------------------------------------------------------------

def _run_hook(hook: Path, event: dict, accounts_dir: Path) -> None:
    env = {"CUS_ACCOUNTS_DIR": str(accounts_dir), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    subprocess.run(
        ["bash", str(hook)],
        input=json.dumps(event),
        text=True,
        env=env,
        check=True,
    )


def test_stopfailure_touches_wake_file_on_real_detection(tmp_path):
    _run_hook(STOP_FAILURE, {
        "hook_event_name": "StopFailure", "session_id": "S1", "error": "rate_limit",
    }, tmp_path)
    assert (tmp_path / "wake-429").exists()


def test_stopfailure_does_not_touch_wake_file_when_not_a_budget_error(tmp_path):
    """authentication_failed isn't budget-relevant and doesn't reach the
    log-append line, so it must not touch wake-429 either."""
    _run_hook(STOP_FAILURE, {
        "hook_event_name": "StopFailure", "session_id": "S2",
        "error": "authentication_failed",
        "error_details": "unrelated rate limit prose in the details field",
    }, tmp_path)
    assert not (tmp_path / "wake-429").exists()
    assert not (tmp_path / "429.log").exists()


def test_ptuf_touches_wake_file_on_real_detection(tmp_path):
    _run_hook(PTUF, {
        "hook_event_name": "PostToolUseFailure", "session_id": "S3",
        "tool_name": "Agent", "tool_input": {"prompt": "do x"},
        "error": 'subagent died: {"type":"error","error":{"type":"rate_limit_error"}}',
    }, tmp_path)
    assert (tmp_path / "wake-429").exists()


def test_ptuf_does_not_touch_wake_file_on_downstream_prose(tmp_path):
    """The 2026-06-23 false-positive shape: a Bash failure mentioning 'rate
    limit' in prose must not log OR touch the wake file."""
    _run_hook(PTUF, {
        "hook_event_name": "PostToolUseFailure", "session_id": "S4",
        "tool_name": "Bash", "tool_input": {"command": "curl ..."},
        "error": "Command failed: server said rate limit exceeded, slow down",
    }, tmp_path)
    assert not (tmp_path / "wake-429").exists()
    assert not (tmp_path / "429.log").exists()


def test_ptuf_does_not_touch_wake_file_when_token_only_in_tool_input(tmp_path):
    _run_hook(PTUF, {
        "hook_event_name": "PostToolUseFailure", "session_id": "S5",
        "tool_name": "Read",
        "tool_input": {"file": "client.py with rate_limit_error handling"},
        "error": "File not found",
    }, tmp_path)
    assert not (tmp_path / "wake-429").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
