"""Tests for GH #76 (+ the GH #75 narrow fix) — swap serialization + crash
journal + daemon single-instance + stale-state lost-update window.

Covered:
  - one global flock serializes execute_swap across processes/entry points
    (contention raises RuntimeError after a timeout; a waiting caller
    proceeds once the holder releases; the lock is released after a swap)
  - write-ahead swap journal: a simulated crash mid-swap leaves the journal
    on disk; the next swap/daemon start detects it and reconciles state
    (completed-live-mutation, never-started, and indeterminate cases)
  - active=None guard: no more `account-None/` creation + post-live-mutation
    crash
  - daemon single-instance flock on daemon.pid
  - GH #75 narrow fix: poll-type writers re-load state.json after the slow
    network phase, so a swap landing mid-poll isn't reverted by the save

Run standalone:  python3 tests/test_swap_lock_journal.py
Or under pytest: pytest tests/test_swap_lock_journal.py
"""

import fcntl
import json
import os
import sys
import tempfile
import threading
import time
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
    at it (same pattern as the other test files). The swap lock + journal
    paths derive from ACCOUNTS_DIR at call time, so repointing ACCOUNTS_DIR
    sandboxes them automatically."""

    def __init__(self, accounts: dict[str, dict], active: str | None, live_creds: dict | bytes,
                 live_identity_of: str | None = None):
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
        ident = live_identity_of if live_identity_of is not None else active
        self.claude_json = root / ".claude.json"
        self.claude_json.write_text(json.dumps({
            "userID": f"uid-{ident}", "oauthAccount": {"emailAddress": f"{ident}@x"},
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
        cus.CONFIG_YAML = accounts_dir / "config.yaml"   # absent → pure defaults
        cus.INBOX_MD = self.inbox_md
        cus.DAEMON_PID = accounts_dir / "daemon.pid"
        cus.migrate_account_dir = lambda d: {"action": "already_migrated"}

    def state(self) -> dict:
        return json.loads(self.state_json.read_text())

    def journal_path(self) -> Path:
        return self.accounts_dir / "swap.journal"

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


TWO_ACCOUNTS = {"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")}


# ---------------------------------------------------------------------------
# Global swap lock
# ---------------------------------------------------------------------------

def test_contended_lock_raises_runtimeerror():
    """A second swap arriving while the lock is held errors out loudly after
    the timeout — RuntimeError, the type every execute_swap caller catches."""
    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-a", "at-a"))
    saved_timeout = cus.SWAP_LOCK_TIMEOUT_SECONDS
    cus.SWAP_LOCK_TIMEOUT_SECONDS = 0.3
    lock_path = env.accounts_dir / "swap.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # play the "other process"
        try:
            cus.execute_swap("b")
            raise AssertionError("expected RuntimeError on lock contention")
        except RuntimeError as e:
            assert "another swap is in flight" in str(e)
        # nothing moved: live files + state untouched
        assert env.state()["active"] == "a"
        assert json.loads(env.creds_json.read_text())["claudeAiOauth"]["refreshToken"] == "rt-a"
    finally:
        cus.SWAP_LOCK_TIMEOUT_SECONDS = saved_timeout
        os.close(fd)
        env.restore()


def test_waiting_caller_serializes_after_holder_releases():
    """The lock BLOCKS (with timeout) rather than failing fast: a swap that
    arrives during another's critical section waits its turn and succeeds."""
    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-a", "at-a"))
    lock_path = env.accounts_dir / "swap.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release_soon():
        time.sleep(0.3)
        fcntl.flock(fd, fcntl.LOCK_UN)

    t = threading.Thread(target=release_soon)
    t.start()
    try:
        start = time.monotonic()
        cus.execute_swap("b")      # must wait ~0.3s, then proceed
        elapsed = time.monotonic() - start
        assert elapsed >= 0.25, f"swap didn't wait for the lock (took {elapsed:.3f}s)"
        assert env.state()["active"] == "b"
    finally:
        t.join()
        os.close(fd)
        env.restore()


def test_lock_released_and_journal_cleared_after_normal_swap():
    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-a", "at-a"))
    try:
        cus.execute_swap("b")
        assert not env.journal_path().exists(), "journal must be retired after a persisted swap"
        cus.execute_swap("a")      # would deadlock/timeout if the lock leaked
        assert env.state()["active"] == "a"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# Crash journal
# ---------------------------------------------------------------------------

def test_simulated_crash_mid_swap_leaves_journal_then_recovers():
    """Kill the swap at its LAST step (save_state) — the exact #76 problem-2
    window: live files fully moved to the target, state.json still naming the
    old account. The journal must survive the crash, and the next swap must
    reconcile state.active to where the live files actually are."""
    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-a", "at-a"))
    saved_save = cus.save_state

    def crash(state):
        raise RuntimeError("simulated crash (OOM-kill) during save_state")

    cus.save_state = crash
    try:
        try:
            cus.execute_swap("b")
            raise AssertionError("expected the simulated crash to propagate")
        except RuntimeError as e:
            assert "simulated crash" in str(e)
        # crash window state: journal present, live=b, state.json still a
        j = json.loads(env.journal_path().read_text())
        assert (j["from"], j["to"]) == ("a", "b")
        assert json.loads(env.creds_json.read_text())["claudeAiOauth"]["refreshToken"] == "rt-b"
        assert env.state()["active"] == "a"

        cus.save_state = saved_save
        # Any next swap first runs recovery under the lock. Target = current
        # after reconciliation, so this call is recovery + no-op.
        cus.execute_swap("b")
        st = env.state()
        assert st["active"] == "b"
        assert not env.journal_path().exists()
        assert any(h.get("trigger") == "crash-recovery" for h in st["swap_history"])
        assert "crash" in env.inbox_md.read_text()
    finally:
        cus.save_state = saved_save
        env.restore()


def test_crash_before_live_mutation_clears_journal_without_repair():
    """Journal exists but the live files still hold the FROM account — the
    interrupted swap never happened; recovery just retires the journal."""
    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-a", "at-a"))
    try:
        cus.write_json(env.journal_path(), {"from": "a", "to": "b", "ts": cus.now_iso()})
        cus.execute_swap("b")      # recovery runs first, then the real a→b swap
        st = env.state()
        assert st["active"] == "b"
        # no crash-recovery history entry — nothing needed repair
        assert all(h.get("trigger") != "crash-recovery" for h in st["swap_history"])
        assert not env.journal_path().exists()
    finally:
        env.restore()


def test_indeterminate_crash_warns_and_preserves_evidence():
    """Live files match NEITHER side of the journal (identity says some third
    thing, creds lineage matches no snapshot): recovery must not guess. It
    warns with manual steps, renames the journal to *.stale.<ts> (preserve
    the log), posts an inbox entry, and leaves state untouched."""
    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-zzz", "at-zzz"),
               live_identity_of="zzz")
    try:
        cus.write_json(env.journal_path(), {"from": "a", "to": "b", "ts": cus.now_iso()})
        with cus._swap_lock():
            cus._recover_pending_swap()
        assert env.state()["active"] == "a"                    # untouched
        assert not env.journal_path().exists()                 # renamed, not deleted
        stale = list(env.accounts_dir.glob("swap.journal.stale.*"))
        assert len(stale) == 1
        assert json.loads(stale[0].read_text())["to"] == "b"   # evidence preserved
        inbox = env.inbox_md.read_text()
        assert "UNRESOLVED" in inbox and "cus whoami" in inbox
    finally:
        env.restore()


def test_recovery_completes_install_when_creds_lagged_identity():
    """Crash in the window between the live identity write and the creds
    install: live .claude.json says TO, live creds still carry FROM's
    lineage. Recovery must finish the install from TO's snapshot."""
    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-a", "at-a"),
               live_identity_of="b")
    try:
        cus.write_json(env.journal_path(), {"from": "a", "to": "b", "ts": cus.now_iso()})
        with cus._swap_lock():
            cus._recover_pending_swap()
        st = env.state()
        assert st["active"] == "b"
        assert json.loads(env.creds_json.read_text())["claudeAiOauth"]["refreshToken"] == "rt-b"
        assert not env.journal_path().exists()
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# active=None guard
# ---------------------------------------------------------------------------

def test_active_none_refuses_before_any_mutation():
    """A hand-built state.json with active=null used to create account-None/,
    mutate the LIVE files, and crash with KeyError AFTER the damage. Now it
    refuses up front with a RuntimeError callers already catch."""
    env = _Env(TWO_ACCOUNTS, active=None, live_creds=_creds("rt-a", "at-a"))
    try:
        try:
            cus.execute_swap("b")
            raise AssertionError("expected RuntimeError for active=None")
        except RuntimeError as e:
            assert "active" in str(e) and "cus init" in str(e)
        assert not (env.accounts_dir / "account-None").exists()
        # live files untouched
        assert json.loads(env.creds_json.read_text())["claudeAiOauth"]["refreshToken"] == "rt-a"
        assert env.state()["active"] is None
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# Daemon single-instance guard
# ---------------------------------------------------------------------------

def test_daemon_singleton_refuses_second_instance():
    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-a", "at-a"))
    try:
        assert cus._acquire_daemon_singleton() is True
        held_fd = cus._DAEMON_SINGLETON_FD
        # Simulate the second daemon process: an independent fd on the same
        # file (flock treats separate open-file-descriptions as separate
        # owners even within one process, so this models the cross-process
        # conflict faithfully).
        fd2 = os.open(str(cus.DAEMON_PID), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            try:
                fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise AssertionError("second flock unexpectedly succeeded")
            except BlockingIOError:
                pass  # exactly what the second daemon hits → it refuses to start
        finally:
            os.close(fd2)
        # pid recorded for SOS/humans
        assert cus.DAEMON_PID.read_text().strip() == str(os.getpid())
        cus._release_daemon_singleton()
        assert cus._DAEMON_SINGLETON_FD is None
        # slot reusable after release (e.g. daemon restart)
        assert cus._acquire_daemon_singleton() is True
        cus._release_daemon_singleton()
        assert held_fd is not None
    finally:
        cus._release_daemon_singleton()
        env.restore()


# ---------------------------------------------------------------------------
# GH #75 narrow fix: reload-before-save in poll-type writers
# ---------------------------------------------------------------------------

def _usage(pct: float = 10.0) -> "cus.AccountUsage":
    return cus.AccountUsage(five_hour=cus.UsageWindow(pct, None),
                            seven_day=cus.UsageWindow(pct, None))


def test_poll_does_not_revert_concurrent_swap():
    """The #75 timeline, compressed: `cus poll` loads state, then a swap
    commits active=b while the poll is on the network, then poll saves. The
    save must NOT revert active to a (that revert is what primes the next
    swap to destroy the departed account's refresh token)."""
    from click.testing import CliRunner

    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-a", "at-a"))
    saved_poll = cus.poll_account_usage

    def poll_and_concurrently_swap(name: str) -> "cus.AccountUsage":
        # Simulate a swap landing while THIS poll call is on the network:
        # rewrite state.json the way execute_swap's save_state does.
        st = json.loads(env.state_json.read_text())
        if st["active"] != "b":
            st["active"] = "b"
            st["swap_history"].append({"ts": cus.now_iso(), "from": "a", "to": "b",
                                       "trigger": "concurrent-manual"})
            env.state_json.write_text(json.dumps(st))
        return _usage()

    cus.poll_account_usage = poll_and_concurrently_swap
    try:
        result = CliRunner().invoke(cus.cli, ["poll"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        st = env.state()
        assert st["active"] == "b", "poll's save reverted a concurrent swap's active pointer (GH #75)"
        assert any(h.get("trigger") == "concurrent-manual" for h in st["swap_history"]), \
            "poll's save dropped the concurrent swap's history entry (GH #75)"
        # and the poll still did its own job on the fresh state
        assert st["accounts"]["a"]["current_5h_pct"] == 10.0
    finally:
        cus.poll_account_usage = saved_poll
        env.restore()


def test_daemon_cycle_does_not_revert_concurrent_swap():
    """Same lost-update scenario through the daemon's one_cycle (via
    `daemon --once`), whose load→poll→save window is the routinely-armed
    seconds-to-40s one from the issue."""
    from click.testing import CliRunner

    env = _Env(TWO_ACCOUNTS, active="a", live_creds=_creds("rt-a", "at-a"))
    saved = {name: getattr(cus, name) for name in (
        "poll_account_usage", "check_rate_limit_reactive", "diagnose",
        "maybe_write_sos", "_log_decision", "_build_decision_record",
    )}

    def poll_and_concurrently_swap(name: str) -> "cus.AccountUsage":
        st = json.loads(env.state_json.read_text())
        if st["active"] != "b":
            st["active"] = "b"
            st["swap_history"].append({"ts": cus.now_iso(), "from": "a", "to": "b",
                                       "trigger": "concurrent-manual"})
            env.state_json.write_text(json.dumps(st))
        return _usage()

    cus.poll_account_usage = poll_and_concurrently_swap
    # Neutralize cycle side-channels that would touch live paths or tmux.
    cus.check_rate_limit_reactive = lambda state, config: None
    cus.diagnose = lambda state=None, config=None: []
    cus.maybe_write_sos = lambda conditions, state: None
    cus._log_decision = lambda record: None
    cus._build_decision_record = lambda *a, **k: {}
    try:
        result = CliRunner().invoke(cus.cli, ["daemon", "--once", "--no-execute"],
                                    catch_exceptions=False)
        assert result.exit_code == 0, result.output
        st = env.state()
        assert st["active"] == "b", "daemon cycle reverted a concurrent swap's active pointer (GH #75)"
        assert any(h.get("trigger") == "concurrent-manual" for h in st["swap_history"])
    finally:
        for name, fn in saved.items():
            setattr(cus, name, fn)
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
