"""Tests for mcpOAuth preservation across cus credential writes (2026-09-17).

The bug: Claude Code stores remote-MCP-server OAuth tokens (atlassian,
singlemcp, otter-ai, ...) under the top-level "mcpOAuth" key of the SAME
.credentials.json that carries the account's claudeAiOauth. cus installed that
file WHOLESALE at every swap (`atomic_copy(install_src, live_creds_path)`) and
saved it back as raw bytes, so each write replaced the DESTINATION's mcpOAuth
with the SOURCE's — usually a registration-only stub. Sessions kept working
from memory while on disk every mount degraded to stubs, and every NEW session
opened to "needs authentication" (first seen 2026-07-10 jira2a/slot-11;
root-caused 2026-09-17 — cus had never handled mcpOAuth at all).

The fix under test: `_preserve_mcp_oauth(dest, new_creds)` — at every existing
credential-write site the written payload's mcpOAuth becomes the union of the
destination's and the source's entries (live-wins, stub-never-beats-live,
fresher-expiresAt-wins, never-delete). claudeAiOauth is never touched; the
helper never raises (degrades to the source payload unchanged). No new
writers, no new files, no daemon pass (PR #185's canonical store was
deliberately NOT taken — it raced single-use refresh tokens, #104).

mcpOAuth is account-AGNOSTIC (keyed "serverName|configHash", entries carry
their own clientId/refreshToken, no Anthropic-account identity) — that is why
carrying it across an account swap is safe.

Run standalone:  python3 tests/test_mcp_oauth_preserve.py
Or under pytest: pytest tests/test_mcp_oauth_preserve.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

KEY = "atlassian|abc"


def _mcp_entry(token: str, refresh: str = "rt", expires_at: int | None = 9_999_999_999_999,
               server: str = "atlassian") -> dict:
    """A live-shaped mcpOAuth entry (what Claude Code writes after a
    successful browser auth). Salvaged from PR #185's fixtures."""
    e = {
        "serverName": server,
        "serverUrl": f"https://mcp.{server}.example/",
        "clientId": "cid",
        "accessToken": token,
        "refreshToken": refresh,
        "redirectUri": "http://localhost:45454/callback",
        "discoveryState": "ok",
    }
    if expires_at is not None:
        e["expiresAt"] = expires_at
    return e


def _stub_entry(server: str = "atlassian") -> dict:
    """A registration-only stub (what a needs-auth server looks like on disk:
    clientId + discoveryState present, token fields EMPTY)."""
    return {
        "serverName": server,
        "serverUrl": f"https://mcp.{server}.example/",
        "clientId": "cid",
        "clientSecret": "cs",
        "accessToken": "",
        "refreshToken": "",
        "redirectUri": "http://localhost:45454/callback",
        "discoveryState": "ok",
    }


def _account_creds(refresh: str, access: str | None = None, expires_at: int = 2_000_000_000_000,
                   mcp: dict | None = None) -> dict:
    creds = {"claudeAiOauth": {
        "accessToken": access if access is not None else f"at-{refresh}",
        "refreshToken": refresh,
        "expiresAt": expires_at,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }}
    if mcp is not None:
        creds["mcpOAuth"] = mcp
    return creds


def _blank(mcp: dict | None = None) -> dict:
    """The exact GH #141 incident signature (epoch expiresAt + empty token),
    optionally still carrying live MCP tokens — the real-world shape: a
    blank hits claudeAiOauth only."""
    creds = {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}
    if mcp is not None:
        creds["mcpOAuth"] = mcp
    return creds


def _identity(name: str) -> dict:
    return {"userID": f"uid-{name}",
            "oauthAccount": {"emailAddress": f"{name}@x", "accountUuid": f"uuid-{name}"}}


class _Env:
    """Throwaway on-disk tree with every cus path constant repointed at it so
    execute_swap / save-back / the heals run for real against temp files (no
    live-machine mutation). Same setattr + restore() pattern as
    test_creds_backup (swap + backups), test_slot_swap (slots: mount_pids
    stub) and test_auto_heal_blank_live_mount (config `mode`)."""

    def __init__(self, accounts: dict[str, dict], active: str,
                 live_creds: dict | bytes | None, mode: str = "global") -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        (self.claude_dir / "settings.json").write_text("{}")
        self.accounts_dir.mkdir(parents=True)

        self.creds_json = self.claude_dir / ".credentials.json"
        if live_creds is not None:
            raw = live_creds if isinstance(live_creds, bytes) else json.dumps(live_creds).encode()
            self.creds_json.write_bytes(raw)
        self.claude_json = root / ".claude.json"
        self.claude_json.write_text(json.dumps(_identity(active)))

        for name, creds in accounts.items():
            d = self.accounts_dir / f"account-{name}"
            d.mkdir()
            (d / ".credentials.json").write_text(json.dumps(creds))
            (d / ".claude.json").write_text(json.dumps(_identity(name)))

        self.state_json = self.accounts_dir / "state.json"
        self.state_json.write_text(json.dumps({
            "active": active,
            "accounts": {n: {"next_swap_at_pct": 50, "current_5h_pct": 0.0, "current_7d_pct": 0.0}
                         for n in accounts},
            "slots": {},
            "swap_history": [],
        }))
        self.config_yaml = self.accounts_dir / "config.yaml"
        cus.write_yaml(self.config_yaml, {"mode": mode})
        self.inbox_md = self.accounts_dir / "inbox.md"

        self._saved = {k: getattr(cus, k) for k in (
            "HOME", "CLAUDE_DIR", "CREDS_JSON", "CLAUDE_JSON", "ACCOUNTS_DIR",
            "STATE_JSON", "CONFIG_YAML", "INBOX_MD", "migrate_account_dir", "mount_pids")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CREDS_JSON = self.creds_json
        cus.CLAUDE_JSON = self.claude_json
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.state_json
        cus.CONFIG_YAML = self.config_yaml
        cus.INBOX_MD = self.inbox_md
        cus.migrate_account_dir = lambda d: {"action": "already_migrated"}
        cus.mount_pids = lambda mount: []

    def live(self) -> dict:
        return json.loads(self.creds_json.read_text())

    def snapshot_path(self, name: str) -> Path:
        return self.accounts_dir / f"account-{name}" / ".credentials.json"

    def snapshot(self, name: str) -> dict:
        return json.loads(self.snapshot_path(name).read_text())

    def backups(self, name: str) -> list[Path]:
        return sorted((self.accounts_dir / f"account-{name}").glob(".credentials.json.bak.*"))

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True)


# ---------------------------------------------------------------------------
# 1. _mcp_merge unit rules (salvaged from PR #185)
# ---------------------------------------------------------------------------

def test_entry_liveness():
    assert cus._mcp_entry_live(_mcp_entry("tok"))
    # Expired-but-refreshable is still live (Claude Code refreshes silently).
    assert cus._mcp_entry_live(_mcp_entry("", refresh="rt", expires_at=1))
    assert not cus._mcp_entry_live(_stub_entry())
    assert not cus._mcp_entry_live(None)
    assert not cus._mcp_entry_live("garbage")


def test_merge_stub_never_beats_live_and_fresher_wins():
    fresh = _mcp_entry("fresh", expires_at=2_000)
    stale = _mcp_entry("stale", expires_at=1_000)
    dst = {KEY: fresh}
    # A staler live entry must not replace a fresher one...
    assert cus._mcp_merge(dst, {KEY: stale}) == 0
    assert dst[KEY] is fresh
    # ...and a stub must never replace ANY live entry.
    assert cus._mcp_merge(dst, {KEY: _stub_entry()}) == 0
    assert dst[KEY] is fresh
    # A fresher live entry DOES replace a staler one.
    dst2 = {KEY: stale}
    assert cus._mcp_merge(dst2, {KEY: fresh}) == 1
    assert dst2[KEY] == fresh
    # Live replaces a stub.
    dst3 = {KEY: _stub_entry()}
    assert cus._mcp_merge(dst3, {KEY: stale}) == 1
    assert dst3[KEY] == stale


def test_merge_live_only_in_dst_survives_identical_is_noop_never_deletes():
    live = _mcp_entry("tok")
    dst = {KEY: live, "keepme|xyz": _stub_entry("keepme")}
    # Source lacks KEY entirely → dst's live entry survives untouched.
    assert cus._mcp_merge(dst, {"other|1": _mcp_entry("o", server="other")}) == 1
    assert dst[KEY] is live and "keepme|xyz" in dst and "other|1" in dst
    # Identical entry ⇒ 0 changes (idempotent).
    assert cus._mcp_merge(dst, {KEY: dict(live)}) == 0
    assert cus._mcp_merge(dst, {"other|1": _mcp_entry("o", server="other")}) == 0
    assert len(dst) == 3  # never deletes


# ---------------------------------------------------------------------------
# 2. _preserve_mcp_oauth: never raises, never touches claudeAiOauth
# ---------------------------------------------------------------------------

def test_preserve_missing_or_corrupt_dest_returns_new_creds_unchanged():
    new = _account_creds("rt-b", mcp={KEY: _stub_entry()})
    before = _canon(new)
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / ".credentials.json"
        out = cus._preserve_mcp_oauth(missing, new)
        assert _canon(out) == before
        corrupt = Path(tmp) / "corrupt.json"
        corrupt.write_text("{not json")
        out = cus._preserve_mcp_oauth(corrupt, new)
        assert _canon(out) == before
    # None dest (empty-slot install) and garbage dest never raise either.
    assert _canon(cus._preserve_mcp_oauth(None, new)) == before
    assert _canon(cus._preserve_mcp_oauth(12345, new)) == before  # type: ignore[arg-type]
    assert _canon(cus._preserve_mcp_oauth({"claudeAiOauth": "not-a-dict", "mcpOAuth": "nope"}, new)) == before
    # Non-dict new_creds: returned as-is, no exception.
    assert cus._preserve_mcp_oauth({"mcpOAuth": {KEY: _mcp_entry("x")}}, "garbage") == "garbage"  # type: ignore[arg-type]
    # The input object itself is never mutated.
    assert _canon(new) == before


def test_preserve_merges_dest_live_over_source_stub_and_leaves_claude_ai_oauth_byte_identical():
    live_tok = _mcp_entry("LIVE-TOKEN", expires_at=5_000)
    dest = _account_creds("rt-a", mcp={KEY: live_tok, "other|1": _mcp_entry("o", server="other")})
    new = _account_creds("rt-b", mcp={KEY: _stub_entry()})
    new_before = _canon(new)
    cai_before = json.dumps(new["claudeAiOauth"])
    out = cus._preserve_mcp_oauth(dest, new)
    # claudeAiOauth: byte-identical to the SOURCE's (never the destination's).
    assert json.dumps(out["claudeAiOauth"]) == cai_before
    assert out["claudeAiOauth"]["refreshToken"] == "rt-b"
    # mcpOAuth: dest's live entry replaced the source's stub; dest-only key carried.
    assert out["mcpOAuth"][KEY] == live_tok
    assert out["mcpOAuth"]["other|1"]["accessToken"] == "o"
    # Source object untouched (helper returns a copy).
    assert _canon(new) == new_before
    assert new["mcpOAuth"][KEY]["accessToken"] == ""
    # Same via a Path dest.
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / ".credentials.json"
        p.write_text(json.dumps(dest))
        out2 = cus._preserve_mcp_oauth(p, new)
        assert _canon(out2) == _canon(out)
    # Source live + fresher beats dest live: source wins, still no claudeAiOauth change.
    fresher = _account_creds("rt-b", mcp={KEY: _mcp_entry("NEWER", expires_at=9_000)})
    out3 = cus._preserve_mcp_oauth(dest, fresher)
    assert out3["mcpOAuth"][KEY]["accessToken"] == "NEWER"
    assert json.dumps(out3["claudeAiOauth"]) == json.dumps(fresher["claudeAiOauth"])


def test_preserve_no_mcp_anywhere_is_identity():
    """Setups with no mcpOAuth at all must get an identical payload."""
    dest = _account_creds("rt-a")
    new = _account_creds("rt-b")
    out = cus._preserve_mcp_oauth(dest, new)
    assert _canon(out) == _canon(new)
    assert "mcpOAuth" not in out


# ---------------------------------------------------------------------------
# 3. End-to-end swap: live MCP token survives the a→b swap on the mount AND in
#    a's saved-back snapshot; GH #79 backups still created
# ---------------------------------------------------------------------------

def test_swap_preserves_live_mcp_token_on_mount_and_in_saved_back_snapshot():
    live_tok = _mcp_entry("LIVE-TOKEN", expires_at=5_000)
    env = _Env({"a": _account_creds("rt-a", access="at-a-old"),
                "b": _account_creds("rt-b", mcp={KEY: _stub_entry()})},
               active="a",
               live_creds=_account_creds("rt-a", access="at-a-REFRESHED", mcp={KEY: live_tok}))
    try:
        cus.execute_swap("b")
        live = env.live()
        # Account payload is exactly b's snapshot's — the swap itself is unchanged.
        assert live["claudeAiOauth"] == env.snapshot("b")["claudeAiOauth"]
        assert live["claudeAiOauth"]["refreshToken"] == "rt-b"
        # ...but the mount's live MCP token beat b's stub.
        assert live["mcpOAuth"][KEY]["accessToken"] == "LIVE-TOKEN"
        # The save-back carried it into a's snapshot too (with a's refreshed tokens).
        snap_a = env.snapshot("a")
        assert snap_a["claudeAiOauth"]["accessToken"] == "at-a-REFRESHED"
        assert snap_a["mcpOAuth"][KEY]["accessToken"] == "LIVE-TOKEN"
        # b's snapshot (a heal SOURCE, never a write target here) is untouched.
        assert env.snapshot("b")["mcpOAuth"][KEY]["accessToken"] == ""
        # GH #79: both pre-overwrite backups still exist with the old content.
        assert len(env.backups("a")) == 1
        assert json.loads(env.backups("a")[0].read_text())["claudeAiOauth"]["accessToken"] == "at-a-old"
        live_baks = sorted(env.claude_dir.glob(".credentials.json.bak.*"))
        assert len(live_baks) == 1
        assert json.loads(live_baks[0].read_text())["claudeAiOauth"]["accessToken"] == "at-a-REFRESHED"
        # A second swap back to a keeps the token flowing (idempotent round trip).
        cus.execute_swap("a")
        assert env.live()["claudeAiOauth"]["refreshToken"] == "rt-a"
        assert env.live()["mcpOAuth"][KEY]["accessToken"] == "LIVE-TOKEN"
        assert env.snapshot("b")["mcpOAuth"][KEY]["accessToken"] == "LIVE-TOKEN"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# 4. Empty-slot launch install writes exactly the target's payload
# ---------------------------------------------------------------------------

def test_empty_slot_install_writes_exactly_target_payload():
    env = _Env({"alpha": _account_creds("rt-alpha", mcp={KEY: _stub_entry()}),
                "beta": _account_creds("rt-beta")},
               active="beta", live_creds=_account_creds("rt-beta"), mode="per_session")
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        cus.save_state(state)
        assert not (d / ".credentials.json").exists()
        cus.execute_swap("alpha", trigger="launch", slot=name)
        installed = json.loads((d / ".credentials.json").read_text())
        assert _canon(installed) == _canon(env.snapshot("alpha"))
        # Global mount untouched by a slot install.
        assert env.live()["claudeAiOauth"]["refreshToken"] == "rt-beta"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# 5. GH #141 negative: a blank/expired SOURCE is still refused, mount untouched
# ---------------------------------------------------------------------------

def test_blank_source_still_refused_and_mount_untouched():
    live = _account_creds("rt-a", mcp={KEY: _mcp_entry("LIVE-TOKEN")})
    env = _Env({"a": _account_creds("rt-a"), "b": _blank()}, active="a", live_creds=live)
    try:
        before = env.creds_json.read_bytes()
        with pytest.raises(RuntimeError):
            cus.execute_swap("b")
        assert env.creds_json.read_bytes() == before
        assert json.loads(env.state_json.read_text())["active"] == "a"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# 6. Save-back regression: a stub-only mount must not erase a snapshot's live token
# ---------------------------------------------------------------------------

def test_saveback_stub_only_mount_keeps_snapshot_live_token():
    live_tok = _mcp_entry("SNAP-LIVE", expires_at=5_000)
    env = _Env({"alpha": _account_creds("rt-alpha", mcp={KEY: live_tok})},
               active="alpha", live_creds=None)
    try:
        state = cus.load_state()
        mount = env.accounts_dir / "slot-1"
        cus.scaffold_mount_dir(mount)
        (mount / ".credentials.json").write_text(json.dumps(
            _account_creds("rt-alpha", expires_at=3_000_000_000_000, mcp={KEY: _stub_entry()})))
        r = cus.saveback_mount_credentials(mount, "alpha", state)
        assert r["action"] == "saved" and r["account"] == "alpha"
        snap = env.snapshot("alpha")
        # The refreshed account token landed...
        assert snap["claudeAiOauth"]["expiresAt"] == 3_000_000_000_000
        # ...and the snapshot's live MCP token survived the stub-only mount.
        assert snap["mcpOAuth"][KEY]["accessToken"] == "SNAP-LIVE"

        # Same guarantee for the independent-login store path.
        store = env.accounts_dir / "account-alpha" / "login-store.json"
        store.write_text(json.dumps(_account_creds("rt-alpha", mcp={KEY: live_tok})))
        mount_creds = _account_creds("rt-alpha", expires_at=4_000_000_000_000, mcp={KEY: _stub_entry()})
        raw = json.dumps(mount_creds).encode()
        assert cus.saveback_to_login_store("alpha", "slot-1", mount_creds, raw, dest_path=store) == "saved"
        stored = json.loads(store.read_text())
        assert stored["claudeAiOauth"]["expiresAt"] == 4_000_000_000_000
        assert stored["mcpOAuth"][KEY]["accessToken"] == "SNAP-LIVE"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# 7. Heal sites: restore claudeAiOauth from the source, keep the mount's mcpOAuth
# ---------------------------------------------------------------------------

def test_auto_heal_live_mount_restores_account_and_keeps_mount_mcp():
    live_tok = _mcp_entry("MOUNT-LIVE", expires_at=5_000)
    env = _Env({"merkos": _account_creds("rt-merkos", access="at-snap")}, active="merkos",
               live_creds=_blank(mcp={KEY: live_tok}), mode="global")
    try:
        assert cus._auto_heal_live_mount(cus.load_state(), cus.load_config()) is True
        live = env.live()
        assert cus._live_mount_creds_invalid(live) is False
        assert live["claudeAiOauth"] == env.snapshot("merkos")["claudeAiOauth"]
        assert live["mcpOAuth"][KEY]["accessToken"] == "MOUNT-LIVE"
        # Snapshot is a heal SOURCE only — never written.
        assert "mcpOAuth" not in env.snapshot("merkos")
        assert sorted(env.claude_dir.glob(".credentials.json.bak.*")), "expected a live backup"
    finally:
        env.restore()


def test_restore_creds_backup_into_live_keeps_mount_mcp_and_seeds_snapshot_verbatim():
    live_tok = _mcp_entry("MOUNT-LIVE", expires_at=5_000)
    env = _Env({"a": _blank()}, active="a", live_creds=_blank(mcp={KEY: live_tok}))
    try:
        good = _account_creds("rt-a", access="at-good")
        bak = env.accounts_dir / "account-a" / ".credentials.json.bak.20260917T000000.000000Z"
        bak.write_text(json.dumps(good))
        cus.restore_creds_backup("a", bak, into_live=True)
        # Snapshot: storage→storage, verbatim bytes (deliberately unmerged).
        assert env.snapshot_path("a").read_bytes() == bak.read_bytes()
        # Live: account payload from the backup, MCP token from the mount.
        live = env.live()
        assert live["claudeAiOauth"] == good["claudeAiOauth"]
        assert live["mcpOAuth"][KEY]["accessToken"] == "MOUNT-LIVE"
    finally:
        env.restore()


def test_relogin_finish_keeps_mount_mcp():
    """`cus relogin --finish` installs a fresh browser-login snapshot (which
    carries NO mcpOAuth) into the live file — the live MCP tokens must survive."""
    live_tok = _mcp_entry("MOUNT-LIVE", expires_at=5_000)
    env = _Env({"a": _account_creds("rt-a-new", access="at-new", expires_at=3_000_000_000_000)},
               active="a",
               live_creds=_account_creds("rt-a-old", access="at-old", mcp={KEY: live_tok}))
    try:
        cus.finish_active_relogin("a")
        live = env.live()
        assert live["claudeAiOauth"]["refreshToken"] == "rt-a-new"
        assert live["mcpOAuth"][KEY]["accessToken"] == "MOUNT-LIVE"
    finally:
        env.restore()


def test_crash_recovery_completed_install_keeps_mount_mcp():
    """_recover_pending_swap's "landed=to, creds lagged" completion is the same
    merge-then-write as the normal install point."""
    live_tok = _mcp_entry("MOUNT-LIVE", expires_at=5_000)
    env = _Env({"a": _account_creds("rt-a"), "b": _account_creds("rt-b")}, active="a",
               live_creds=_account_creds("rt-a", mcp={KEY: live_tok}))
    try:
        # Simulate a crash after the identity write but before the creds copy:
        # journal says a→b, live .claude.json already carries b, creds still a's.
        cus._write_swap_journal("a", "b", "manual")
        env.claude_json.write_text(json.dumps(_identity("b")))
        cus._recover_pending_swap()
        live = env.live()
        assert live["claudeAiOauth"]["refreshToken"] == "rt-b"
        assert live["mcpOAuth"][KEY]["accessToken"] == "MOUNT-LIVE"
    finally:
        env.restore()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
