"""GH #199: the peer registry (`sessions/<pid>.json`) must be SHARED across mounts.

Claude Code discovers sibling sessions — what ListAgents lists and what
SendMessage addresses — by reading `<CLAUDE_CONFIG_DIR>/sessions/<pid>.json`.
Because every cus slot mount owned a private real `sessions/` dir, a session
launched with `cus launch` and a bare session were mutually invisible: no error
at launch, the peer name simply never resolved.

Covers:
  - scaffold_mount_dir: a fresh mount is born with sessions/ symlinked, even
    when the shared registry doesn't exist yet (slots-only box)
  - doctor_mount dry run: reports the drift, changes nothing
  - doctor_mount --fix: live entries adopted into the shared registry, dead /
    colliding / non-file entries parked (move, never delete), dir relinked
  - the structural effect: after the heal, a file written through the mount
    path is visible at the shared path and vice versa (this is what makes
    cross-mount session mail work)
  - idempotency: a healed mount re-runs clean

Run standalone:  python3 tests/test_sessions_peer_registry.py
Run under pytest: pytest tests/test_sessions_peer_registry.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

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
        # Liveness is /proc ground truth in production; tests declare it, so no
        # real process has to be spawned (and no real pid can be misread).
        self._saved_alive = cus._pid_alive
        self.live_pids: set[int] = set()
        cus._pid_alive = lambda pid: pid in self.live_pids

    @property
    def shared_sessions(self) -> Path:
        return self.claude_dir / "sessions"

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus._pid_alive = self._saved_alive
        self._tmp.cleanup()


def _reg(pid: int) -> str:
    """A minimal peer-registry payload, shaped like Claude Code's."""
    return json.dumps({"pid": pid, "sessionId": f"sess-{pid}", "cwd": "/home/user/repo"})


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


def test_doctor_dry_run_reports_but_changes_nothing():
    env = _Env()
    try:
        real = env.mount / "sessions"
        real.mkdir()
        (real / "111.json").write_text(_reg(111))
        env.live_pids = {111}

        findings = cus.doctor_mount(env.mount, fix=False)
        entries = [f for f in findings if f["entry"] == "sessions"]
        assert entries, "dry run reports the unshared peer registry"
        assert not entries[0]["healed"]
        assert (real / "111.json").exists(), "dry run moved nothing"
        assert not (env.mount / "sessions").is_symlink()
        assert not list(env.shared_sessions.iterdir())
    finally:
        env.restore()


def test_doctor_adopts_live_parks_dead_and_relinks():
    env = _Env()
    try:
        real = env.mount / "sessions"
        real.mkdir()
        (real / "111.json").write_text(_reg(111))        # live → adopt
        (real / "222.json").write_text(_reg(222))        # dead → park
        (real / "333.json").write_text(_reg(333))        # live but collides → park
        (real / "junk.txt").write_text("not a registry")  # unnameable → park
        (real / "subdir").mkdir()                         # not a file → park
        env.live_pids = {111, 333}
        # The shared registry already maintains 333 — Claude Code owns that
        # copy, so the mount's must never overwrite it.
        (env.shared_sessions / "333.json").write_text(_reg(333))

        findings = cus.doctor_mount(env.mount, fix=True)
        entry = next(f for f in findings if f["entry"] == "sessions")
        assert entry["healed"], entry

        link = env.mount / "sessions"
        assert link.is_symlink()
        assert link.resolve() == env.shared_sessions.resolve()
        assert (env.shared_sessions / "111.json").exists(), "live session adopted into shared registry"
        assert json.loads((env.shared_sessions / "333.json").read_text())["pid"] == 333

        parks = list(env.mount.glob("sessions.bak-*"))
        assert len(parks) == 1, "one dated park dir"
        parked = {p.name for p in parks[0].iterdir()}
        assert parked == {"222.json", "333.json", "junk.txt", "subdir"}, parked
        assert not (env.shared_sessions / "222.json").exists(), "dead pid never pollutes the shared registry"
    finally:
        env.restore()


def test_healed_mount_sees_shared_registry_both_ways():
    """The structural proof behind the fix: once sessions/ is a symlink, a
    registration written by a BARE session (straight into ~/.claude/sessions/)
    is readable through the slot mount's path, and one written through the
    mount is readable bare. That shared view is what ListAgents reads."""
    env = _Env()
    try:
        cus.doctor_mount(env.mount, fix=True)
        link = env.mount / "sessions"
        assert link.is_symlink()

        (env.shared_sessions / "444.json").write_text(_reg(444))   # "bare" session
        assert (link / "444.json").exists(), "bare registration visible from the slot mount"

        (link / "555.json").write_text(_reg(555))                  # "slotted" session
        assert (env.shared_sessions / "555.json").exists(), "slot registration visible bare"
    finally:
        env.restore()


def test_doctor_is_idempotent():
    env = _Env()
    try:
        real = env.mount / "sessions"
        real.mkdir()
        (real / "111.json").write_text(_reg(111))
        env.live_pids = {111}
        cus.doctor_mount(env.mount, fix=True)
        assert cus.doctor_mount(env.mount, fix=True) == [], "healed mount re-runs clean"
    finally:
        env.restore()


def test_registry_pid_falls_back_to_body():
    """A truncated or oddly-named entry must not be guessed dead: the pid in
    the body is the fallback, and an unreadable file yields None (→ parked,
    never adopted, never deleted)."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "999.json").write_text(_reg(999))
        (d / "odd-name.json").write_text(_reg(777))
        (d / "broken.json").write_text("{ truncated")
        assert cus._session_registry_pid(d / "999.json") == 999
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
