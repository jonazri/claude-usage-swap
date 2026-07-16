"""Task 28 (spec-2 token-pressure forecaster, STAGE 1): cross-repo INSTALL
GATE, cus (H1) half -- a missing/unreachable `sentinel` CLI/socket degrades
`_pressure_cycle`'s LIVE (`shadow_mode: false`) emit path to log-only,
NEVER a crash. The H2 (sentineld) half of Task 28 is a separate follow-up
in the sentinel repo and is out of scope here.

`_sentinel_available()` gates every admitted emit inside `_pressure_cycle`'s
LIVE branch, independent of `shadow_mode` (a missing dependency is always
log-only, even when `shadow_mode: false` -- see cus.py's own `_pressure_
degrade_to_log_only`/`_pressure_emit_to_sentinel` docstrings). This holds
whether the daemon is offered no `sentinel` binary at all (PATH lookup
fails) or a present binary with an absent/unreachable authed emit socket
(Task 32's real server is Stage 2, not built yet) -- both degrade the same
way: log the would-emit at WARN, append it to the shadow log, and return
`_pressure_cycle` cleanly with no exception raised, so the daemon's
forecasting loop keeps running regardless.

HARNESS (same as every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file; add the repo root to ``sys.path`` and
``import cus``. `_pressure_cycle` takes `state`/`config` as EXPLICIT
parameters (no internal disk read), so only its OWN pressure-owned
artifacts (pressure.json, the shadow log, the last-emit registry -- all
rooted under `PRESSURE_ROOT`) plus the NEW `SENTINEL_ROOT` env var (Task 28
-- resolves `_sentinel_emit_socket_path()`) need isolating per test.

Run: ``python3 -m pytest tests/test_pressure_install_gate.py -q``.
"""

import json
import logging
import os
import shutil
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


# Same fleet shape every other tests/test_pressure_*.py file uses:
# reference_x pinned to 5 (FACT #4), one 20x account ("A") gives ratio 4.0.
BASE_CFG = {
    "capacity_aware": {"enabled": True, "reference_x": 5},
    "thresholds": {"steps": [70, 85, 94]},   # gate_5h = 94 (live ladder top)
    "per_model_weekly": {"cap_pct": 95},
    "accounts": [{"name": "A", "capacity_x": 20}],
}


def _acct(pct=96.0, pct7d=10.0):
    # last_poll_ts set so the pool view is never spuriously release-
    # suppressed; pct=96 > gate_5h=94 is an immediate (eta=0.0) breach
    # regardless of burn rate -- the same minimal breaching fixture
    # tests/test_pressure_shadow.py's test_shadow_false_still_emits uses.
    return {"capacity_x": 20, "current_5h_pct": pct, "current_7d_pct": pct7d,
            "last_poll_ts": NOW.isoformat()}


def _env(tmp_path, monkeypatch, sentinel_root=None):
    """Isolated tmp tree for `_pressure_cycle` (mirrors tests/
    test_pressure_shadow.py's `_env`) PLUS Task 28's `SENTINEL_ROOT`
    isolation -- `_sentinel_emit_socket_path()` resolves off this env var
    (falling back to `~/claude-accounts/sentinel-runtime` otherwise), so a
    test that doesn't isolate it could accidentally read a real operator
    install on the machine running these tests."""
    monkeypatch.setattr(cus, "CLAUDE_DIR", tmp_path / "claude_home")
    monkeypatch.setattr(cus, "SESSIONS_LOG", tmp_path / "sessions.log")

    accounts_dir = tmp_path / "claude-accounts"
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(cus, "PRESSURE_JSON", accounts_dir / "pressure.json")
    monkeypatch.setattr(cus, "PRESSURE_ROOT", accounts_dir / "pressure")
    monkeypatch.setattr(cus, "_PRESSURE_TAIL_OFFSETS", {})

    monkeypatch.setenv("SENTINEL_ROOT", str(sentinel_root or (tmp_path / "sentinel-runtime")))

    return accounts_dir


def _shadow_log_path(accounts_dir: Path, now: datetime) -> Path:
    return accounts_dir / "pressure" / "shadow" / f"{now:%Y-%m-%d}.jsonl"


def _breaching_cycle_args():
    state = {"accounts": {"A": _acct()}}
    config = dict(BASE_CFG, pressure={"shadow_mode": False})
    return state, config


def test_sentinel_absent_emit_is_log_only(tmp_path, monkeypatch, caplog):
    """`sentinel` absent on PATH (`shutil.which` -> None) + `shadow_mode:
    false` + a breaching state -> the emit client is NEVER reached; the
    would-emit is logged at WARN and appended to the shadow log;
    `_pressure_cycle` returns cleanly with no exception; the daemon keeps
    forecasting (the caller gets a real snapshot back, not a crash)."""
    accounts_dir = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    client_calls = []
    monkeypatch.setattr(cus, "_pressure_emit_to_sentinel",
                         lambda payload, config: client_calls.append(payload) or True)

    state, config = _breaching_cycle_args()

    caplog.set_level(logging.WARNING)
    snapshot = cus._pressure_cycle(state, config, NOW)   # must not raise

    assert snapshot["level"] != "ok"
    assert client_calls == [], "the emit client must never be reached when sentinel is absent"

    assert any(r.levelno == logging.WARNING for r in caplog.records), \
        "the would-emit must be logged at WARN"

    log_path = _shadow_log_path(accounts_dir, NOW)
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["would_emit"] is not None
    assert record["would_emit"]["severity"] == snapshot["level"]
    assert record["degraded_reason"] == "sentinel_unavailable"

    # Nothing was actually emitted -- the live last_emit.json hysteresis
    # must stay untouched so a real emit isn't silently suppressed once
    # sentinel becomes available.
    assert not (accounts_dir / "pressure" / "last_emit.json").exists()


def test_sentinel_present_socket_unreachable_degrades(tmp_path, monkeypatch, caplog):
    """`sentinel` found on PATH, but the authed emit socket is a stale
    non-socket file (present but unconnectable) -> same log-only degrade,
    no crash. Uses a genuinely short SENTINEL_ROOT (AF_UNIX sun_path is
    capped ~108 bytes) so `socket.connect()` exercises a real ECONNREFUSED
    rather than an unrelated "path too long" OSError."""
    with tempfile.TemporaryDirectory() as short_root:
        accounts_dir = _env(tmp_path, monkeypatch, sentinel_root=Path(short_root))
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/sentinel")

        sock_path = Path(short_root) / "run" / "emit.sock"
        sock_path.parent.mkdir(parents=True)
        sock_path.write_text("")   # present, but not a bound listening socket

        client_calls = []
        monkeypatch.setattr(cus, "_pressure_emit_to_sentinel",
                             lambda payload, config: client_calls.append(payload) or True)

        state, config = _breaching_cycle_args()

        caplog.set_level(logging.WARNING)
        snapshot = cus._pressure_cycle(state, config, NOW)   # must not raise

        assert snapshot["level"] != "ok"
        assert client_calls == [], "the emit client must never be reached when the socket is unreachable"
        assert any(r.levelno == logging.WARNING for r in caplog.records)

        log_path = _shadow_log_path(accounts_dir, NOW)
        lines = log_path.read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["would_emit"] is not None
        assert record["degraded_reason"] == "sentinel_unavailable"


def test_degrade_independent_of_shadow_mode(tmp_path, monkeypatch):
    """The unavailable -> log-only degrade holds regardless of
    `shadow_mode` -- a missing dependency is ALWAYS log-only. Runs the SAME
    breaching state through two isolated cycles, one with the default
    `shadow_mode` (true) and one with `shadow_mode: false`, sentinel absent
    in both: neither ever reaches the emit client, and BOTH leave a
    would-emit record in their own shadow log."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    client_calls = []
    monkeypatch.setattr(cus, "_pressure_emit_to_sentinel",
                         lambda payload, config: client_calls.append(payload) or True)

    state = {"accounts": {"A": _acct()}}

    accounts_dir_true = _env(tmp_path / "default_shadow", monkeypatch)
    snap_true = cus._pressure_cycle(state, BASE_CFG, NOW)   # shadow_mode defaults True
    log_true = json.loads(_shadow_log_path(accounts_dir_true, NOW).read_text().splitlines()[-1])

    accounts_dir_false = _env(tmp_path / "explicit_false", monkeypatch)
    config_false = dict(BASE_CFG, pressure={"shadow_mode": False})
    snap_false = cus._pressure_cycle(state, config_false, NOW)
    log_false = json.loads(_shadow_log_path(accounts_dir_false, NOW).read_text().splitlines()[-1])

    assert client_calls == [], "emit client must never be reached in either mode when sentinel is absent"
    assert snap_true["level"] != "ok"
    assert snap_false["level"] != "ok"
    assert log_true["would_emit"] is not None
    assert log_false["would_emit"] is not None
    assert log_false["degraded_reason"] == "sentinel_unavailable"


def test_sentinel_available_true_reaches_client(tmp_path, monkeypatch):
    """When `_sentinel_available()` reads True, `_pressure_cycle`'s LIVE
    branch DOES reach the emit client for a real breach under
    `shadow_mode: false` -- proving the gate is a real, two-sided branch
    (not a permanent no-op) and that a successful live emit does NOT also
    write a log-only degrade record."""
    accounts_dir = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(cus, "_sentinel_available", lambda: True)

    client_calls = []
    monkeypatch.setattr(cus, "_pressure_emit_to_sentinel",
                         lambda payload, config: client_calls.append(payload) or True)
    monkeypatch.setattr(cus, "_pressure_write_emit_marker", lambda key, payload, now: None)

    state, config = _breaching_cycle_args()
    snapshot = cus._pressure_cycle(state, config, NOW)

    assert snapshot["level"] != "ok"
    assert len(client_calls) == 1
    assert client_calls[0]["severity"] == snapshot["level"]

    log_path = _shadow_log_path(accounts_dir, NOW)
    assert not log_path.exists(), "an available, successful emit must not write a log-only degrade record"

    registry_path = accounts_dir / "pressure" / "last_emit.json"
    assert registry_path.exists()
    registry = json.loads(registry_path.read_text())
    assert "token-pressure:account:A:5h" in registry


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
