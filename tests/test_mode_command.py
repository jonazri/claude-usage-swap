"""Tests for Phase 4: cus mode transitions + per_session SOS conditions.

Plan: docs/plans/2026-07-02-per-session-accounts.md (4.1 + 4.2).

Run standalone:  python3 tests/test_mode_command.py
Run under pytest: pytest tests/test_mode_command.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from click.testing import CliRunner  # noqa: E402

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
        (self.claude_dir / ".credentials.json").write_text(json.dumps(_creds("rt-alpha")))
        self.claude_json.write_text(json.dumps(_identity("alpha")))

        for name in ("alpha", "beta"):
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{name}")))
            (d / ".claude.json").write_text(json.dumps(_identity(name)))

        cus.write_json(self.accounts_dir / "state.json", {
            "active": "alpha",
            "accounts": {n: {"next_swap_at_pct": 50} for n in ("alpha", "beta")},
            "swap_history": [],
        })
        # config.yaml with a comment that a naive YAML round-trip would destroy
        (self.accounts_dir / "config.yaml").write_text(
            "# hand-written comment that must survive mode flips\n"
            "poll_interval_seconds: 180\n"
        )

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
        cus._OCCUPIED_SLOTS_CACHE.clear()

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus._OCCUPIED_SLOTS_CACHE.clear()
        self._tmp.cleanup()


def test_mode_roundtrip_preserves_config_comments():
    env = _Env()
    try:
        runner = CliRunner()
        r = runner.invoke(cus.cli, ["mode"])
        assert r.exit_code == 0 and "global" in r.output

        r = runner.invoke(cus.cli, ["mode", "per-session"])
        assert r.exit_code == 0, r.output
        text = cus.CONFIG_YAML.read_text()
        assert "mode: per_session" in text
        assert "# hand-written comment that must survive mode flips" in text
        assert cus.load_config()["mode"] == "per_session"
        assert cus.list_slot_dirs(), "first slot created on transition"

        r = runner.invoke(cus.cli, ["mode", "global"])
        assert r.exit_code == 0, r.output
        text = cus.CONFIG_YAML.read_text()
        assert "mode: global" in text
        assert "# hand-written comment that must survive mode flips" in text
        assert not cus.list_slot_dirs(), "idle slots reaped on transition to global"
    finally:
        env.restore()


def test_mode_per_session_refuses_broken_pool():
    env = _Env()
    try:
        # beta's snapshot loses its refresh token → validation must refuse.
        (env.accounts_dir / "account-beta" / ".credentials.json").write_text("{}")
        runner = CliRunner()
        r = runner.invoke(cus.cli, ["mode", "per-session"])
        assert r.exit_code == 1
        assert "no refresh token" in r.output
        assert cus.load_config().get("mode", "global") == "global", "config not flipped on refusal"
    finally:
        env.restore()


def test_mode_global_refuses_live_slots_without_force():
    env = _Env()
    try:
        runner = CliRunner()
        assert runner.invoke(cus.cli, ["mode", "per-session"]).exit_code == 0
        cus.mount_pids = lambda mount: [1]  # every slot looks live
        r = runner.invoke(cus.cli, ["mode", "global"])
        assert r.exit_code == 1 and "exit them first" in r.output
        r = runner.invoke(cus.cli, ["mode", "global", "--force"])
        assert r.exit_code == 0, r.output
        assert cus.load_config()["mode"] == "global"
    finally:
        env.restore()


def test_sos_flags_slot_drift_and_orphans():
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        # State claims beta, but the slot's live identity is alpha → drift.
        state["slots"][name]["account"] = "beta"
        cus.write_json(d / ".claude.json", _identity("alpha"))
        cus.save_state(state)

        conditions = cus.diagnose(cus.load_state(), cus.load_config())
        assert any("drift" in c.summary and name in c.summary for c in conditions), \
            [c.summary for c in conditions]

        # Orphan: slot dir with creds, no state entry.
        orphan = env.accounts_dir / "slot-9"
        orphan.mkdir()
        (orphan / ".credentials.json").write_text(json.dumps(_creds("rt-alpha")))
        conditions = cus.diagnose(cus.load_state(), cus.load_config())
        assert any("Orphan" in c.summary and "slot-9" in c.summary for c in conditions)
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
