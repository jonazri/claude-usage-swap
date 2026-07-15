"""Tests for SOS.md write de-duplication (self-retrigger loop fix).

`maybe_write_sos` used to call `atomic_write_bytes` UNCONDITIONALLY whenever
`conditions` was non-empty. `atomic_write_bytes` uses `os.replace()` (which
bumps mtime on every call) and the body embeds a volatile `_Updated <ts>_`
line (so the content also differs every call). Because the function is invoked
from both the daemon poll loop and the `cus sos` CLI verify step, any
non-self-healing SOS condition made an mtime-based file_watch re-fire
indefinitely (~17x on the 2026-07-14 incident).

The fix makes the function a genuine NO-OP when the SUBSTANTIVE conditions are
unchanged since the last write, comparing bodies with the volatile timestamp
line normalized out.

Run standalone:  python3 tests/test_sos_dedup.py
Or under pytest: pytest tests/test_sos_dedup.py
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _cond(severity="urgent", summary="acct A blocked", action="run cus relogin A"):
    return cus.SOSCondition(
        severity=severity, summary=summary, action=action, affected="A"
    )


def _setup(tmp_path, monkeypatch):
    """Point SOS.md/LAST_NOTIFY at tmp, make now_iso() return monotonically
    DIFFERENT timestamps (so only the volatile `_Updated` line would ever
    differ between two calls with identical substantive conditions), and
    neutralize the desktop-notify side effect."""
    monkeypatch.setattr(cus, "SOS_MD", tmp_path / "SOS.md")
    monkeypatch.setattr(cus, "LAST_NOTIFY", tmp_path / ".last_notify.json")
    counter = {"n": 0}

    def fake_now():
        counter["n"] += 1
        return f"2026-07-14T00:00:{counter['n']:02d}Z"

    monkeypatch.setattr(cus, "now_iso", fake_now)
    # Never actually pop a desktop notification during the test run.
    monkeypatch.setattr(cus.shutil, "which", lambda _name: None)


def _pin_old_mtime(path):
    """Set mtime to a known-old value so any rewrite (os.replace) is
    detectable via st_mtime_ns without sleeping."""
    old = time.time() - 1000
    os.utime(path, (old, old))
    return path.stat().st_mtime_ns


def test_first_write_creates_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert not cus.SOS_MD.exists()
    cus.maybe_write_sos([_cond()], {})
    assert cus.SOS_MD.exists()
    body = cus.SOS_MD.read_bytes()
    assert b"[URGENT] acct A blocked" in body
    assert b"run cus relogin A" in body


def test_no_rewrite_when_conditions_unchanged(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    cus.maybe_write_sos([_cond()], {})
    first_bytes = cus.SOS_MD.read_bytes()
    old_mtime_ns = _pin_old_mtime(cus.SOS_MD)

    # Identical substantive conditions → genuine no-op (only the volatile
    # timestamp would differ, and that must NOT trigger a rewrite).
    cus.maybe_write_sos([_cond()], {})

    assert cus.SOS_MD.stat().st_mtime_ns == old_mtime_ns, (
        "SOS.md mtime was bumped despite unchanged substantive conditions"
    )
    assert cus.SOS_MD.read_bytes() == first_bytes, (
        "SOS.md bytes changed despite unchanged substantive conditions"
    )


def test_rewrite_when_condition_changes(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    cus.maybe_write_sos([_cond(summary="acct A blocked")], {})
    first_bytes = cus.SOS_MD.read_bytes()
    old_mtime_ns = _pin_old_mtime(cus.SOS_MD)

    # A genuinely changed condition MUST rewrite.
    cus.maybe_write_sos([_cond(summary="acct B blocked")], {})

    assert cus.SOS_MD.stat().st_mtime_ns != old_mtime_ns, (
        "SOS.md was not rewritten despite a changed substantive condition"
    )
    body = cus.SOS_MD.read_bytes()
    assert body != first_bytes
    assert b"acct B blocked" in body


def test_rewrite_when_action_only_changes(tmp_path, monkeypatch):
    """The action text is substantive too (not just severity/summary)."""
    _setup(tmp_path, monkeypatch)
    cus.maybe_write_sos([_cond(action="run cus relogin A")], {})
    old_mtime_ns = _pin_old_mtime(cus.SOS_MD)

    cus.maybe_write_sos([_cond(action="run cus force-poll A")], {})

    assert cus.SOS_MD.stat().st_mtime_ns != old_mtime_ns, (
        "SOS.md was not rewritten despite a changed action string"
    )
    assert b"run cus force-poll A" in cus.SOS_MD.read_bytes()


def test_empty_conditions_removes_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    cus.maybe_write_sos([_cond()], {})
    assert cus.SOS_MD.exists()
    cus.maybe_write_sos([], {})
    assert not cus.SOS_MD.exists()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
