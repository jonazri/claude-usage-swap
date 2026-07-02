"""Tests for Phase 2.1: mount-parameterized swap (execute_swap slot=...).

Plan: docs/plans/2026-07-02-per-session-accounts.md.

The load-bearing assertions:
  - a slot swap NEVER touches the global mount (~/.claude/ + ~/.claude.json)
    or state.active — that isolation is the whole point of per_session mode
  - swap-into-empty-slot (cus launch's install primitive) works with no
    outgoing account: no save-back, no ladder bump
  - occupied-slot swap runs the full guard stack: save-back to the outgoing
    account dir, ladder bump, slot-tagged history
  - crash recovery reconciles slot journals against the SLOT's files, not
    the global mount's

Run standalone:  python3 tests/test_slot_swap.py
Run under pytest: pytest tests/test_slot_swap.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _creds(refresh: str, expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": f"at-{refresh}", "refreshToken": refresh, "expiresAt": expires_at}}


def _identity(name: str) -> dict:
    return {"userID": f"uid-{name}", "oauthAccount": {"emailAddress": f"{name}@x", "accountUuid": f"uuid-{name}"}}


class _Env:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.claude_json = root / ".claude.json"
        self.accounts_dir = root / "claude-accounts"

        (self.claude_dir / "projects").mkdir(parents=True)
        (self.claude_dir / "settings.json").write_text("{}")
        # Global mount holds gamma — an account that never participates in the
        # slot swaps below, so any change to these files is a bug caught by
        # global_mount_unchanged().
        (self.claude_dir / ".credentials.json").write_text(json.dumps(_creds("rt-gamma")))
        self.claude_json.write_text(json.dumps({**_identity("gamma"), "mcpServers": {"m": {}}, "numStartups": 7}))

        for name in ("alpha", "beta", "gamma"):
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{name}")))
            (d / ".claude.json").write_text(json.dumps(_identity(name)))

        cus.write_json(self.accounts_dir / "state.json", {
            "active": "gamma",
            "accounts": {n: {"next_swap_at_pct": 50} for n in ("alpha", "beta", "gamma")},
            "swap_history": [],
        })

        self._saved = {k: getattr(cus, k) for k in
                       ("HOME", "CLAUDE_DIR", "CLAUDE_JSON", "CREDS_JSON", "ACCOUNTS_DIR", "STATE_JSON", "CONFIG_YAML")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CLAUDE_JSON = self.claude_json
        cus.CREDS_JSON = self.claude_dir / ".credentials.json"
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.accounts_dir / "state.json"
        cus.CONFIG_YAML = self.accounts_dir / "config.yaml"
        self._saved_mount_pids = cus.mount_pids
        cus.mount_pids = lambda mount: []

        self._global_before = self._global_snapshot()

    def _global_snapshot(self) -> tuple[str, str]:
        return ((self.claude_dir / ".credentials.json").read_text(), self.claude_json.read_text())

    def global_mount_unchanged(self) -> bool:
        return self._global_snapshot() == self._global_before

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        self._tmp.cleanup()


def test_swap_into_empty_slot_installs_without_touching_global():
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        cus.save_state(state)

        state = cus.execute_swap("alpha", trigger="launch", slot=name)

        assert json.loads((d / ".credentials.json").read_text())["claudeAiOauth"]["refreshToken"] == "rt-alpha"
        slot_cj = json.loads((d / ".claude.json").read_text())
        assert slot_cj["userID"] == "uid-alpha"
        assert state["slots"][name]["account"] == "alpha"
        assert state["active"] == "gamma", "global active untouched by a slot swap"
        assert env.global_mount_unchanged(), "global mount files untouched"
        # No outgoing account → no ladder bump anywhere.
        assert all(a["next_swap_at_pct"] == 50 for a in state["accounts"].values())
        assert state["swap_history"][-1]["slot"] == name
        assert state["swap_history"][-1]["from"] is None
    finally:
        env.restore()


def test_occupied_slot_swap_saves_back_and_bumps_ladder():
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        cus.save_state(state)
        cus.execute_swap("alpha", trigger="launch", slot=name)

        # Session refreshed alpha's tokens in the slot (fresher expiresAt).
        (d / ".credentials.json").write_text(json.dumps(_creds("rt-alpha", expires_at=3_000_000_000_000)))

        state = cus.execute_swap("beta", trigger="threshold", slot=name)

        # Outgoing alpha's refreshed tokens saved back to its account dir.
        snap = json.loads((env.accounts_dir / "account-alpha" / ".credentials.json").read_text())
        assert snap["claudeAiOauth"]["expiresAt"] == 3_000_000_000_000
        # Beta installed into the slot.
        assert json.loads((d / ".credentials.json").read_text())["claudeAiOauth"]["refreshToken"] == "rt-beta"
        assert json.loads((d / ".claude.json").read_text())["userID"] == "uid-beta"
        # Ladder bumped for the outgoing account only.
        assert state["accounts"]["alpha"]["next_swap_at_pct"] == 75
        assert state["accounts"]["beta"]["next_swap_at_pct"] == 50
        assert state["slots"][name]["account"] == "beta"
        assert state["active"] == "gamma"
        assert env.global_mount_unchanged()
    finally:
        env.restore()


def test_slot_swap_same_account_is_noop():
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        cus.save_state(state)
        cus.execute_swap("alpha", trigger="launch", slot=name)
        before = (d / ".credentials.json").read_text()
        state = cus.execute_swap("alpha", trigger="threshold", slot=name)
        assert (d / ".credentials.json").read_text() == before
        assert len([h for h in state["swap_history"] if h.get("slot") == name]) == 1
    finally:
        env.restore()


def test_slot_swap_preserves_nonaccount_keys_in_slot_cj():
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        cus.save_state(state)
        cus.write_json(d / ".claude.json", {"mcpServers": {"local": {}}, "numStartups": 3})
        cus.execute_swap("alpha", trigger="launch", slot=name)
        cj = json.loads((d / ".claude.json").read_text())
        assert cj["mcpServers"] == {"local": {}}, "surgical merge: non-account keys survive the install"
        assert cj["userID"] == "uid-alpha"
    finally:
        env.restore()


def test_crash_recovery_slot_journal_completes_install():
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        state["slots"][name]["account"] = "alpha"
        cus.save_state(state)
        # Simulate a crash mid-swap alpha→beta on this slot: identity written,
        # creds not yet installed (still alpha's).
        (d / ".credentials.json").write_text(json.dumps(_creds("rt-alpha")))
        cus.write_json(d / ".claude.json", _identity("beta"))
        cus._write_swap_journal("alpha", "beta", "threshold", slot=name)

        cus._recover_pending_swap()

        assert not cus._swap_journal_path().exists()
        assert json.loads((d / ".credentials.json").read_text())["claudeAiOauth"]["refreshToken"] == "rt-beta", \
            "recovery completed the creds install"
        state = cus.load_state()
        assert state["slots"][name]["account"] == "beta"
        assert state["active"] == "gamma", "global active never involved"
        assert env.global_mount_unchanged()
    finally:
        env.restore()


def test_crash_recovery_empty_slot_journal_clears_quietly():
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        cus.save_state(state)
        # Crash before anything landed: slot cj is the seeded {}, no creds.
        cus._write_swap_journal(None, "beta", "launch", slot=name)

        cus._recover_pending_swap()

        assert not cus._swap_journal_path().exists(), "journal cleared"
        assert not list(env.accounts_dir.glob(".swap-journal.json.stale.*")), "not escalated to stale"
        state = cus.load_state()
        assert state["slots"][name]["account"] is None
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
