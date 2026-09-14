"""GH #199: the peer registry (`sessions/<pid>.json` + `<pid>.<hash>.key`) must be SHARED.

Claude Code discovers sibling sessions — what ListAgents lists and what
SendMessage addresses — by reading `<CLAUDE_CONFIG_DIR>/sessions/`. Each live
session publishes a PAIR: `<pid>.json` (metadata) and `<pid>.<sha256>.key`
(`peerToken` / `pidDomain` / `procStart`). Because every cus slot mount owned a
private real `sessions/` dir, a session launched with `cus launch` and a bare
session were mutually invisible.

Covers (fix pass 1 / dual-review F-B-1..7, F-A-1..3):
  - scaffold paths create shared sessions/ and symlink it
  - doctor dry run: reports drift, changes nothing
  - live pair → deferred (files untouched, not relinked)
  - dead / recycled-pid pairs + orphan files → parked as units, then relink
  - ENOTEMPTY on rmdir → re-scan / defer, no sweep crash
  - wrong-target sessions symlink repointed
  - healed=False / doctor exit 1 on deferred

Run standalone:  python3 tests/test_sessions_peer_registry.py
Run under pytest: pytest tests/test_sessions_peer_registry.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


class _Env:
    """Throwaway tree mirroring the production mount layout.

    Same monkeypatch-constants pattern as test_slots_doctor_sync._Env so this
    file stays standalone-runnable. NOTHING here touches the real
    ~/.claude/ or ~/claude-accounts/ — the live peer registry is exactly the
    state this fix must not corrupt.
    """

    def __init__(self, with_shared_sessions: bool = True) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        for sub in ("projects", "plugins"):
            (self.claude_dir / sub).mkdir(parents=True)
        if with_shared_sessions:
            (self.claude_dir / "sessions").mkdir(parents=True)
        (self.claude_dir / "settings.json").write_text("{}")
        self.mount = self.accounts_dir / "slot-1"
        self.mount.mkdir(parents=True)
        (self.mount / ".claude.json").write_text("{}")

        self._saved = {k: getattr(cus, k) for k in ("HOME", "CLAUDE_DIR", "ACCOUNTS_DIR")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.ACCOUNTS_DIR = self.accounts_dir
        # Liveness is /proc + procStart in production; tests declare it so no
        # real process has to be spawned (and no real pid can be misread).
        # Signature matches cus._pid_alive(pid, proc_start=None).
        self._saved_alive = cus._pid_alive
        self.live_pids: set[int] = set()
        cus._pid_alive = lambda pid, proc_start=None: pid in self.live_pids

    @property
    def shared_sessions(self) -> Path:
        return self.claude_dir / "sessions"

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus._pid_alive = self._saved_alive
        self._tmp.cleanup()


def _reg(pid: int, proc_start: str = "1000") -> str:
    """A minimal peer-registry .json payload, shaped like Claude Code's."""
    return json.dumps({
        "pid": pid,
        "sessionId": f"sess-{pid}",
        "cwd": "/home/user/repo",
        "procStart": proc_start,
        "pidDomain": "linux:test",
    })


def _key(proc_start: str = "1000") -> str:
    """Companion .key payload — peerToken/pidDomain/procStart, no pid (F-B-1)."""
    return json.dumps({
        "peerToken": "tok-" + proc_start,
        "pidDomain": "linux:test",
        "procStart": proc_start,
    })


def _write_pair(directory: Path, pid: int, proc_start: str = "1000",
                key_hash: str = "abc") -> None:
    (directory / f"{pid}.json").write_text(_reg(pid, proc_start))
    (directory / f"{pid}.{key_hash}.key").write_text(_key(proc_start))


def test_scaffold_links_sessions_even_when_shared_dir_absent():
    """A box driven entirely through slots has never run a bare session, so
    ~/.claude/sessions/ does not exist. Without the explicit create, the
    no-dangling-links rule would skip sessions/ and the new mount would grow
    its own private registry — the bug, re-created at every slot creation."""
    env = _Env(with_shared_sessions=False)
    try:
        mount = env.accounts_dir / "slot-2"
        cus.scaffold_mount_dir(mount)
        assert env.shared_sessions.is_dir(), "shared registry created on demand"
        assert (mount / "sessions").is_symlink()
        assert (mount / "sessions").resolve() == env.shared_sessions.resolve()
    finally:
        env.restore()


def test_login_scaffolds_create_shared_sessions_and_symlink():
    """F-B-3 / F-A-2: login-store and login-family scaffolds must not skip
    sessions/ on a slots-only box."""
    env = _Env(with_shared_sessions=False)
    try:
        store = cus.scaffold_login_store_dir("acct", "slot-1")
        fam = cus.scaffold_login_family_dir("acct", "family-1")
        assert env.shared_sessions.is_dir()
        assert (store / "sessions").is_symlink()
        assert (fam / "sessions").is_symlink()
        assert (store / "sessions").resolve() == env.shared_sessions.resolve()
        assert (fam / "sessions").resolve() == env.shared_sessions.resolve()
    finally:
        env.restore()


def test_login_family_scaffold_converts_empty_real_sessions_dir():
    """F-B-4: re-scaffold replaces an empty real sessions/ with the symlink."""
    env = _Env()
    try:
        fam = env.accounts_dir / "logins" / "acct" / "family-1"
        fam.mkdir(parents=True)
        (fam / "sessions").mkdir()
        cus.scaffold_login_family_dir("acct", "family-1")
        link = fam / "sessions"
        assert link.is_symlink()
        assert link.resolve() == env.shared_sessions.resolve()
    finally:
        env.restore()


def test_doctor_dry_run_reports_but_changes_nothing():
    env = _Env()
    try:
        real = env.mount / "sessions"
        real.mkdir()
        _write_pair(real, 111)
        env.live_pids = {111}

        findings = cus.doctor_mount(env.mount, fix=False)
        entries = [f for f in findings if f["entry"] == "sessions"]
        assert entries, "dry run reports the unshared peer registry"
        assert not entries[0]["healed"]
        assert (real / "111.json").exists(), "dry run moved nothing"
        assert (real / "111.abc.key").exists()
        assert not (env.mount / "sessions").is_symlink()
        assert not list(env.shared_sessions.iterdir())
    finally:
        env.restore()


def test_live_pair_defers_conversion_and_moves_nothing():
    """F-B-1 / F-B-7: a live session's json+key stay put; mount is not relinked."""
    env = _Env()
    try:
        real = env.mount / "sessions"
        real.mkdir()
        _write_pair(real, 111)
        _write_pair(real, 222, key_hash="dead")
        env.live_pids = {111}

        findings = cus.doctor_mount(env.mount, fix=True)
        entry = next(f for f in findings if f["entry"] == "sessions")
        assert not entry["healed"], entry
        assert "deferred (live sessions: pids 111)" in entry["action"], entry["action"]
        assert not (env.mount / "sessions").is_symlink()
        assert (real / "111.json").exists()
        assert (real / "111.abc.key").exists()
        assert (real / "222.json").exists(), "deferral moves nothing, including dead pairs"
        assert not list(env.mount.glob("sessions.bak-*"))
        assert not list(env.shared_sessions.iterdir())
    finally:
        env.restore()


def test_dead_and_recycled_pairs_parked_then_relinked():
    """No live pairs → park complete pairs + orphans as units, then relink.

    A recycled pid (kill would succeed in production but procStart wouldn't
    match) is declared not-live by the monkeypatch → parked, not deferred.
    """
    env = _Env()
    try:
        real = env.mount / "sessions"
        real.mkdir()
        _write_pair(real, 222)                          # dead pair → park
        _write_pair(real, 333, key_hash="recycled")      # recycled → park
        (real / "junk.txt").write_text("not a registry")  # orphan → park
        (real / "orphan.key").write_text(_key())          # orphan key → park
        (real / "subdir").mkdir()
        env.live_pids = set()  # nothing live (recycled pid not in set)

        findings = cus.doctor_mount(env.mount, fix=True)
        entry = next(f for f in findings if f["entry"] == "sessions")
        assert entry["healed"], entry
        assert "adopted 0 pairs" in entry["action"], entry["action"]
        assert "parked 2 pairs" in entry["action"], entry["action"]
        assert "parked 3 orphan files" in entry["action"], entry["action"]
        assert "relinked" in entry["action"]

        link = env.mount / "sessions"
        assert link.is_symlink()
        assert link.resolve() == env.shared_sessions.resolve()
        assert not list(env.shared_sessions.iterdir()), "dead pairs never pollute shared"

        parks = list(env.mount.glob("sessions.bak-*"))
        assert len(parks) == 1
        parked = {p.name for p in parks[0].iterdir()}
        assert parked == {
            "222.json", "222.abc.key",
            "333.json", "333.recycled.key",
            "junk.txt", "orphan.key", "subdir",
        }, parked
    finally:
        env.restore()


def test_enotempty_rescan_defers_without_raising():
    """F-B-5: rmdir ENOTEMPTY → one re-scan; then succeed — never abort."""
    import errno as errno_mod

    env = _Env()
    try:
        real = env.mount / "sessions"
        real.mkdir()
        _write_pair(real, 222)
        env.live_pids = set()

        state = {"rmdir_hits": 0}
        real_rmdir = Path.rmdir

        def flaky_rmdir(self):
            # Only interfere with this mount's sessions dir.
            if self == real and state["rmdir_hits"] == 0:
                state["rmdir_hits"] += 1
                (self / "racer.json").write_text(_reg(999))
                raise OSError(errno_mod.ENOTEMPTY, "Directory not empty", str(self))
            return real_rmdir(self)

        with mock.patch.object(Path, "rmdir", flaky_rmdir):
            findings = cus.doctor_mount(env.mount, fix=True)

        entry = next(f for f in findings if f["entry"] == "sessions")
        assert state["rmdir_hits"] == 1
        assert entry["healed"], entry
        assert (env.mount / "sessions").is_symlink()
        parks = list(env.mount.glob("sessions.bak-*"))
        assert parks and any(p.name == "racer.json" for p in parks[0].iterdir())
    finally:
        env.restore()


def test_wrong_target_sessions_symlink_repointed():
    """F-A-3: sessions symlink pointing elsewhere is repointed."""
    env = _Env()
    try:
        other = env.accounts_dir / "other-sessions"
        other.mkdir()
        (env.mount / "sessions").symlink_to(other)
        findings = cus.doctor_mount(env.mount, fix=True)
        entry = next(f for f in findings if f["entry"] == "sessions")
        assert entry["healed"]
        link = env.mount / "sessions"
        assert link.is_symlink()
        assert link.resolve() == env.shared_sessions.resolve()
    finally:
        env.restore()


def test_healed_mount_sees_shared_registry_both_ways():
    """Once sessions/ is a symlink, registrations are visible both ways."""
    env = _Env()
    try:
        cus.doctor_mount(env.mount, fix=True)
        link = env.mount / "sessions"
        assert link.is_symlink()

        (env.shared_sessions / "444.json").write_text(_reg(444))
        assert (link / "444.json").exists(), "bare registration visible from the slot mount"

        (link / "555.json").write_text(_reg(555))
        assert (env.shared_sessions / "555.json").exists(), "slot registration visible bare"
    finally:
        env.restore()


def test_doctor_is_idempotent():
    env = _Env()
    try:
        real = env.mount / "sessions"
        real.mkdir()
        _write_pair(real, 222)
        env.live_pids = set()
        cus.doctor_mount(env.mount, fix=True)
        assert cus.doctor_mount(env.mount, fix=True) == [], "healed mount re-runs clean"
    finally:
        env.restore()


def test_doctor_cmd_exits_nonzero_on_deferred_live():
    """F-B-7 / GH #192: unhealed findings make doctor exit 1."""
    env = _Env()
    try:
        real = env.mount / "sessions"
        real.mkdir()
        _write_pair(real, 111)
        env.live_pids = {111}
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(cus.cli, ["doctor", "--fix-sessions"])
        assert result.exit_code == 1, result.output
        assert "deferred" in result.output
    finally:
        env.restore()


def test_doctor_cmd_dry_run_flag_writes_nothing():
    env = _Env()
    try:
        real = env.mount / "sessions"
        real.mkdir()
        _write_pair(real, 222)
        env.live_pids = set()
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(cus.cli, ["doctor", "--fix-sessions", "--dry-run"])
        assert result.exit_code == 1, result.output  # findings present
        assert (real / "222.json").exists()
        assert not (env.mount / "sessions").is_symlink()
    finally:
        env.restore()


def test_pid_alive_rejects_nonpositive_and_requires_proc_start():
    """F-B-9 / F-B-2: pid<=0 never live; missing procStart never live."""
    assert cus._pid_alive(0, "1") is False
    assert cus._pid_alive(-1, "1") is False
    assert cus._pid_alive(1, None) is False


def test_registry_pid_from_key_prefix_and_json_fallback():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        key_name = "999." + ("a" * 64) + ".key"
        (d / "999.json").write_text(_reg(999))
        (d / key_name).write_text(_key())
        (d / "odd-name.json").write_text(_reg(777))
        (d / "broken.json").write_text("{ truncated")
        assert cus._session_registry_pid(d / "999.json") == 999
        assert cus._session_registry_pid(d / key_name) == 999
        assert cus._session_registry_pid(d / "odd-name.json") == 777
        assert cus._session_registry_pid(d / "broken.json") is None


def _run_all() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run_all()
    print("all sessions-peer-registry tests passed")
