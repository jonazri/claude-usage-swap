"""Shared pytest fixtures for the whole `tests/` suite.

Fix wave 1, Part 2 (test-isolation defect): `_pressure_cycle`'s own
module-level path constants — `PRESSURE_ROOT`, `PRESSURE_JSON`, `CLAUDE_DIR`,
`SESSIONS_LOG` (all computed from `Path.home()` at cus.py import time, see
cus.py's "Paths" section near the top of the file) — are NOT covered by the
daemon tests' own `_Env` sandbox helpers (test_swap_lock_journal.py's and
test_pressure_daemon_wire.py's `_Env` classes only repoint `ACCOUNTS_DIR`/
`STATE_JSON`/`CREDS_JSON`/`CLAUDE_JSON`/`CONFIG_YAML`/`INBOX_MD`/
`DAEMON_PID` — see either `_Env` docstring). A daemon test that drives a
real `daemon --once` cycle without also monkeypatching `cus._pressure_cycle`
itself therefore falls through, via the fix-wave-1 choke point, to the REAL
machine paths (`~/claude-accounts/pressure/`, `~/.claude`) — a confirmed
pollution incident:
`tests/test_swap_lock_journal.py::test_daemon_cycle_does_not_revert_concurrent_swap`
does exactly this today, and writing real data to the operator's actual
pressure store (`weight_window_cursor.json`, `session_rate_history.jsonl`,
...) from a unit test corrupts Task 27b's live weight-fit accumulator.

The autouse fixture below defaults EVERY test in this directory onto a
per-test tmp tree for those four constants — not just the tests that already
know to sandbox them — so no test can write to or read from the real
`~/claude-accounts/pressure/` or `~/.claude` regardless of whether its own
setup remembers to. Tests that need specific pressure paths (e.g.
tests/test_pressure_cli.py's own `_env()` helper) already
`monkeypatch.setattr(cus, ...)` these same four constants explicitly inside
the test body, which runs AFTER this fixture's setup — `monkeypatch`'s undo
stack unwinds in reverse regardless of how many times the same attribute
was set, so those tests' own values simply take over for the remainder of
the test and teardown still correctly restores the true pre-test originals
afterward.

No sibling pressure-store path constants exist to also patch: every other
pressure-owned artifact (`weight_windows.jsonl`, `weight_window_cursor.json`,
`session_rate_history.jsonl`, `last_emit.json`, `last_would_emit.json`, the
shadow-log dir, ...) is computed CALL-TIME from `PRESSURE_ROOT` via small
helpers (`_pressure_weight_window_cursor_path()` and friends in cus.py,
grepped and confirmed while writing this fixture) rather than its own
module constant, so repointing `PRESSURE_ROOT` alone sandboxes all of them
too — the same precedent `replay_forecast`'s `_REPLAY_PATCHED_GLOBALS` and
test_pressure_cli.py's `_env()` already rely on.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


@pytest.fixture(autouse=True)
def _sandbox_pressure_paths(tmp_path, monkeypatch):
    """Autouse, every test: repoint cus.py's pressure-owned path constants
    at a per-test tmp tree before the test body runs. `mkdir`'d up front
    (not just pointed at a not-yet-existing path) so a forecaster cycle
    that actually executes (rather than being spied/mocked) writes its
    atomic tmp+rename output harmlessly under `tmp_path` instead of
    failing on a missing directory or, worse, silently resolving back to
    a real path.
    """
    claude_dir = tmp_path / "_pressure_sandbox_claude_home"
    accounts_dir = tmp_path / "_pressure_sandbox_claude_accounts"
    pressure_root = accounts_dir / "pressure"
    claude_dir.mkdir(parents=True, exist_ok=True)
    pressure_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(cus, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(cus, "SESSIONS_LOG", accounts_dir / "sessions.log")
    monkeypatch.setattr(cus, "PRESSURE_JSON", accounts_dir / "pressure.json")
    monkeypatch.setattr(cus, "PRESSURE_ROOT", pressure_root)
