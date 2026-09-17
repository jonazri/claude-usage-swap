"""Pins `_prefer_as_oom_victim()` (GH #221, 2026-09-17 fleet-crash fix).

The recurring fleet-wide crash was earlyoom SIGTERMing the `systemd --user`
manager: on Ubuntu `user@.service` sets OOMScoreAdjust=100 (inherited by every
user unit, incl. the ~11MB manager), while a `cus`-launched `claude` ran at
adj 0 — a LOWER-priority OOM victim than the manager. `_prefer_as_oom_victim()`
raises the launching process's oom_score_adj (default 500) right before it execs
claude, so a runaway session dies alone instead of taking the whole fleet.

These tests exercise the helper directly (the real exec paths can't be driven in
a unit test), pinning the three behaviours the incident fix depends on:
  1. it writes the configured value to /proc/self/oom_score_adj,
  2. it defaults to "500" (matching ~/bin/claude-pane-launcher) when the env is unset,
  3. a failed/denied write is swallowed and NEVER propagates (best-effort: it must
     never block a launch).
"""

from __future__ import annotations

import builtins
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cus  # noqa: E402

OOM_PATH = "/proc/self/oom_score_adj"


class _CapturingFile(io.StringIO):
    """A StringIO that records what was written to it on context-manager exit,
    so we can assert the value without touching the real /proc file."""

    def __init__(self, sink: dict) -> None:
        super().__init__()
        self._sink = sink

    def __enter__(self) -> "_CapturingFile":
        return self

    def __exit__(self, *exc) -> bool:
        self._sink["written"] = self.getvalue()
        return False  # don't suppress exceptions


def _patch_oom_open(monkeypatch, sink: dict, raise_exc: Exception | None = None) -> None:
    """Redirect only opens of the oom_score_adj path; everything else is real."""
    real_open = builtins.open

    def fake_open(path, mode="r", *args, **kwargs):
        if str(path) == OOM_PATH:
            sink["path"], sink["mode"] = str(path), mode
            if raise_exc is not None:
                raise raise_exc
            return _CapturingFile(sink)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)


def test_writes_default_500_when_env_unset(monkeypatch):
    monkeypatch.delenv("CUS_PANE_OOM_SCORE_ADJ", raising=False)
    sink: dict = {}
    _patch_oom_open(monkeypatch, sink)
    cus._prefer_as_oom_victim()
    assert sink.get("path") == OOM_PATH
    assert sink.get("written") == "500"


def test_writes_configured_env_value(monkeypatch):
    monkeypatch.setenv("CUS_PANE_OOM_SCORE_ADJ", "750")
    sink: dict = {}
    _patch_oom_open(monkeypatch, sink)
    cus._prefer_as_oom_victim()
    assert sink.get("written") == "750"


def test_write_failure_is_swallowed(monkeypatch):
    """A denied/failed write must degrade to prior behaviour, never raise —
    otherwise a /proc quirk could block a launch, which is the opposite of the goal."""
    monkeypatch.setenv("CUS_PANE_OOM_SCORE_ADJ", "500")
    sink: dict = {}
    _patch_oom_open(monkeypatch, sink, raise_exc=OSError("EACCES: denied"))
    # Must return normally despite the OSError.
    cus._prefer_as_oom_victim()
