"""Tests for the spec-2 Stage-1 deploy-time integration fix: wiring the
token-pressure forecaster's daemon cycle (`_pressure_cycle`) into the daemon
loop (`daemon()` / its nested `one_cycle()`).

Before this fix, `_pressure_cycle` was defined but never called by anything
in the daemon — the shadow-mode forecaster never ran in production. This
file proves two things:

  1. `_pressure_cycle` is actually invoked, once per daemon cycle, with the
     daemon's live `(state, config, now)` — regardless of which of
     `one_cycle()`'s several account-management branches (reactive-429 /
     per_session / hybrid / hold / lazy-defer / no-execute / executed-swap)
     is the one that fired this cycle. `one_cycle()` has NO single tail —
     it returns from six different points plus one implicit fall-through —
     so the wiring calls a small guarded helper, `_pressure_cycle_safe`,
     from every one of those exit points.
  2. THE MOST IMPORTANT PROPERTY: a forecaster fault can NEVER crash or
     disrupt account rotation. If `_pressure_cycle` raises, `one_cycle()`
     must still complete normally (account work already happened), the
     exception must never propagate out of `daemon --once`, and a WARNING
     must be logged.

Both tests monkeypatch `cus._pressure_cycle` itself (spy / raiser) rather
than letting the real forecaster run — the real function reads real
transcripts under `CLAUDE_DIR`/`SESSIONS_LOG` and writes real
`PRESSURE_JSON`/`PRESSURE_ROOT` files, none of which this file's `_Env`
sandboxes (only `ACCOUNTS_DIR`-derived paths are sandboxed, matching every
other daemon test in this suite — see test_swap_lock_journal.py). Letting
the real `_pressure_cycle` run here would exercise real, unsandboxed
machine paths, which is neither what this file is testing (the WIRING, not
the forecaster internals — those already have dedicated test_pressure_*.py
coverage) nor safe to do from a generic unit test.

Run standalone:  python3 tests/test_pressure_daemon_wire.py
Or under pytest: pytest tests/test_pressure_daemon_wire.py
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _creds(refresh: str, access: str) -> dict:
    return {"claudeAiOauth": {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": 9999999999999,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }}


class _Env:
    """Throwaway on-disk account tree with every cus path constant repointed
    at it (same pattern as test_swap_lock_journal.py / the other daemon
    tests). The swap lock + journal paths derive from ACCOUNTS_DIR at call
    time, so repointing ACCOUNTS_DIR sandboxes them automatically. Note:
    CLAUDE_DIR/SESSIONS_LOG/PRESSURE_JSON/PRESSURE_ROOT are NOT sandboxed by
    this class (they're independent module-level constants bound to the real
    HOME at import time, not derived from ACCOUNTS_DIR) — every test in this
    file relies on monkeypatching `cus._pressure_cycle` itself instead of
    letting it touch those real paths."""

    def __init__(self, accounts: dict[str, dict], active: str | None, live_creds: dict | bytes):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        accounts_dir = root / "claude-accounts"
        claude_dir = root / ".claude"
        claude_dir.mkdir(parents=True)
        accounts_dir.mkdir(parents=True)

        self.claude_dir = claude_dir
        self.creds_json = claude_dir / ".credentials.json"
        raw = live_creds if isinstance(live_creds, bytes) else json.dumps(live_creds).encode()
        self.creds_json.write_bytes(raw)
        self.claude_json = root / ".claude.json"
        self.claude_json.write_text(json.dumps({
            "userID": f"uid-{active}", "oauthAccount": {"emailAddress": f"{active}@x"},
        }))

        self.accounts_dir = accounts_dir
        for name, creds in accounts.items():
            d = accounts_dir / f"account-{name}"
            d.mkdir()
            (d / ".credentials.json").write_text(json.dumps(creds))
            (d / ".claude.json").write_text(json.dumps({
                "userID": f"uid-{name}", "oauthAccount": {"emailAddress": f"{name}@x"},
            }))

        self.state_json = accounts_dir / "state.json"
        self.state_json.write_text(json.dumps({
            "active": active,
            "accounts": {n: {"next_swap_at_pct": 50} for n in accounts},
            "swap_history": [],
        }))
        self.inbox_md = accounts_dir / "inbox.md"

        self._saved = {k: getattr(cus, k) for k in (
            "ACCOUNTS_DIR", "STATE_JSON", "CREDS_JSON", "CLAUDE_JSON",
            "CONFIG_YAML", "INBOX_MD", "DAEMON_PID", "migrate_account_dir",
        )}
        cus.ACCOUNTS_DIR = accounts_dir
        cus.STATE_JSON = self.state_json
        cus.CREDS_JSON = self.creds_json
        cus.CLAUDE_JSON = self.claude_json
        cus.CONFIG_YAML = accounts_dir / "config.yaml"   # absent -> pure defaults
        cus.INBOX_MD = self.inbox_md
        cus.DAEMON_PID = accounts_dir / "daemon.pid"
        cus.migrate_account_dir = lambda d: {"action": "already_migrated"}

    def state(self) -> dict:
        return json.loads(self.state_json.read_text())

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


TWO_ACCOUNTS = {"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")}


def _usage(pct: float = 10.0) -> "cus.AccountUsage":
    return cus.AccountUsage(five_hour=cus.UsageWindow(pct, None),
                             seven_day=cus.UsageWindow(pct, None))


class _DaemonHarness:
    """Neutralizes the same account-management side channels
    test_swap_lock_journal.py's daemon test does (poll/reactive/diagnose/SOS/
    decision-logging), so `daemon --once` can run end-to-end against the
    sandboxed `_Env` without touching real accounts, tmux panes, or
    filesystem paths outside the sandbox. `poll_pct` controls the polled
    usage — either a single float applied to every account (below every
    threshold -> hold), or a `{account: pct}` dict for per-account control
    (e.g. one account above 90% and a fresh target -> a real swap
    decision)."""

    def __init__(self, poll_pct: "float | dict[str, float]" = 10.0):
        self._poll_pct = poll_pct
        self._saved = {name: getattr(cus, name) for name in (
            "poll_account_usage", "check_rate_limit_reactive", "diagnose",
            "maybe_write_sos", "_log_decision", "_build_decision_record",
        )}

    def __enter__(self):
        def _poll(name: str) -> "cus.AccountUsage":
            pct = self._poll_pct.get(name, 10.0) if isinstance(self._poll_pct, dict) else self._poll_pct
            return _usage(pct)
        cus.poll_account_usage = _poll
        cus.check_rate_limit_reactive = lambda state, config: None
        cus.diagnose = lambda state=None, config=None: []
        cus.maybe_write_sos = lambda conditions, state: None
        cus._log_decision = lambda record: None
        cus._build_decision_record = lambda *a, **k: {}
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(cus, name, fn)


def test_daemon_cycle_calls_pressure_cycle():
    """`daemon --once` (a full one_cycle() run) invokes `_pressure_cycle`
    exactly once, with the daemon's own state/config/now — proving the
    forecaster is genuinely wired into the loop, not just defined."""
    from click.testing import CliRunner

    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-a", "at-a"))
    calls = []
    saved_pressure_cycle = cus._pressure_cycle

    def spy(state, config, now):
        calls.append((state, config, now))
        return {}

    cus._pressure_cycle = spy
    try:
        with _DaemonHarness(poll_pct=10.0):  # well under every threshold -> hold branch
            result = CliRunner().invoke(cus.cli, ["daemon", "--once", "--no-execute"],
                                        catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert len(calls) == 1, f"expected exactly one _pressure_cycle call per cycle, got {len(calls)}"
        state_arg, config_arg, now_arg = calls[0]
        assert isinstance(state_arg, dict) and "accounts" in state_arg
        assert isinstance(config_arg, dict)
        assert isinstance(now_arg, datetime), f"expected a datetime, got {type(now_arg)}"
    finally:
        cus._pressure_cycle = saved_pressure_cycle
        env.restore()


def test_pressure_cycle_failure_never_crashes_daemon():
    """THE most important test: `_pressure_cycle` raising must NOT crash
    `daemon --once`, must NOT prevent the account-management work from
    completing, and must produce a WARNING in the daemon's own output —
    proving the fail-safe guard around the forecaster call actually holds."""
    from click.testing import CliRunner

    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-a", "at-a"))
    saved_pressure_cycle = cus._pressure_cycle
    cus._pressure_cycle = lambda state, config, now: (_ for _ in ()).throw(
        RuntimeError("boom: simulated forecaster fault"))
    try:
        with _DaemonHarness(poll_pct=10.0):
            result = CliRunner().invoke(cus.cli, ["daemon", "--once", "--no-execute"],
                                        catch_exceptions=False)
        # The exception must never propagate out of the daemon command.
        assert result.exit_code == 0, (
            f"a _pressure_cycle fault propagated out of daemon --once "
            f"(exit={result.exit_code}):\n{result.output}"
        )
        # The account-management cycle must still have run to completion —
        # the cycle-start banner is only printed once account work begins.
        assert "cycle start." in result.output, (
            "account-management work did not appear to run at all:\n" + result.output
        )
        # The fault must be visible as a WARNING, not silently dropped.
        assert "[WARNING]" in result.output and "token-pressure cycle failed" in result.output, (
            "expected a WARNING about the forecaster fault in daemon output:\n" + result.output
        )
        assert "boom: simulated forecaster fault" in result.output
    finally:
        cus._pressure_cycle = saved_pressure_cycle
        env.restore()


def test_pressure_cycle_called_on_no_execute_swap_branch():
    """Bonus coverage: one_cycle() has no single tail (six early returns +
    one fall-through). This drives a SECOND, different branch — the active
    account above threshold with a fresh swap target available, run with
    --no-execute, so a real swap is decided but skipped — to confirm the
    wiring at that exit point too, not just the hold branch the two tests
    above exercise."""
    from click.testing import CliRunner

    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-a", "at-a"))
    calls = []
    saved_pressure_cycle = cus._pressure_cycle
    cus._pressure_cycle = lambda state, config, now: calls.append(1) or {}
    try:
        # "a" (active) above THRESHOLD_STEPS[-2]=90, "b" fresh -> a real
        # swap target exists and gets decided, unlike both-maxed (which
        # falls back to a no-target hold, not the no-execute skip branch).
        with _DaemonHarness(poll_pct={"a": 95.0, "b": 10.0}):
            result = CliRunner().invoke(cus.cli, ["daemon", "--once", "--no-execute"],
                                        catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "skipping actual swap" in result.output, (
            "expected the no-execute swap-skip branch to fire:\n" + result.output
        )
        assert len(calls) == 1, f"expected exactly one _pressure_cycle call, got {len(calls)}"
    finally:
        cus._pressure_cycle = saved_pressure_cycle
        env.restore()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
