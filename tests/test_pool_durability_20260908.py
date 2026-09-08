"""Tests for the 2026-09-07/08 login-family pool-collapse fixes.

Background: Anthropic refresh-token families have a ~28-30 day ABSOLUTE
lifetime (rotation does not extend it). Every family in the pool was minted
Aug 6-7, so they hit the wall together; that cliff was then AMPLIFIED into a
6/8-lane logout by three cus bugs this suite pins down:

  FIX 1 — crash recovery must never "complete" a REFUSED swap.
    The swap journal (and the live .claude.json identity) used to be written
    BEFORE the install-source refusal guards; a refused swap therefore left a
    journal + target identity + outgoing tokens, which `_recover_pending_swap`
    read as "crashed after the live mutation" and finished with a RAW snapshot
    copy that bypassed every guard — installing the dead/colliding token the
    swap had just refused.
      (1a) a refused swap leaves NO journal and NO identity drift on the mount;
      (1b) recovery REFUSES to complete an install whose target is live on
           another mount (#104) — identity rolled back, tokens untouched,
           evidence preserved as swap.journal.refused.<ts>;
      (1c) recovery REFUSES a DEAD target snapshot (invalid_grant);
      (1d) recovery still completes a HEALTHY install (regression guard);
      (1e) the shared-mount (slot=None) path refuses a dead target too;
      (1f) `_live_mount_creds_invalid` treats expired-access + no-refresh as
           invalid (disk-only widening), leaving the other shapes unchanged.

  FIX 2 — generation-transfer heal (`_lane_heal_source`):
    a pooled lane whose leased family store is well-shaped-but-DEAD used to be
    healed from that same dead store (heal->blank->heal loop) while the account
    CANONICAL sat alive. Now the lane is re-seeded from the canonical via
    `_reseed_family_from_canonical` (grant + persist), re-leased, and the dead
    family retired — but ONLY when the canonical's token family is not live on
    any other mount (#104), and only behind the config gate.

  FIX 3 — hygiene / visibility:
    `_free_family_count` no longer counts past-wall / disk-dead families as
    free; `cus status` shows per-family age + days-to-wall; the family-age SOS
    is URGENT for an account backing a LIVE lane once a family is within
    `urgent_wall_within_days` of the wall.

Run standalone:  python3 tests/test_pool_durability_20260908.py
Or under pytest: pytest tests/test_pool_durability_20260908.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402

_FUTURE = 2_000_000_000_000   # ~2033: an unexpired access token
_PAST = 1_000_000_000_000     # 2001: an expired access token


def _valid(access: str, refresh: str, expires_at: int = _FUTURE) -> dict:
    return {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh,
                              "expiresAt": expires_at}}


def _expired(refresh: str) -> dict:
    """Well-SHAPED (non-blank) but expired access token: `_live_mount_creds_invalid`
    reads it as valid-shaped; only the refresh grant can tell dead from alive."""
    return {"claudeAiOauth": {"accessToken": "at-expired", "refreshToken": refresh,
                              "expiresAt": _PAST}}


def _blank() -> dict:
    return {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}


def _identity(name: str) -> dict:
    return {"userID": f"uid-{name}",
            "oauthAccount": {"accountUuid": f"uuid-{name}", "emailAddress": f"{name}@x"}}


class _Env:
    """Throwaway on-disk tree with every cus path constant repointed at it, plus a
    /proc live-mount mock, click.echo capture and a patch registry. Mirrors
    test_dead_snapshot_family_seed._Env (slots + families + grant map)."""

    def __init__(self, accounts: dict[str, dict], active: str,
                 config: dict | None = None, live_identity_of: str | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.accounts_dir.mkdir(parents=True)

        self.creds_json = self.claude_dir / ".credentials.json"
        self.creds_json.write_text(json.dumps(accounts[active]))
        self.claude_json = root / ".claude.json"
        self.claude_json.write_text(json.dumps(_identity(live_identity_of or active)))

        for name, creds in accounts.items():
            d = self.accounts_dir / f"account-{name}"
            d.mkdir()
            (d / ".credentials.json").write_text(json.dumps(creds))
            (d / ".claude.json").write_text(json.dumps(_identity(name)))

        self.state_json = self.accounts_dir / "state.json"
        self.state_json.write_text(json.dumps({
            "active": active,
            "accounts": {n: {"next_swap_at_pct": 50, "current_5h_pct": 0.0,
                             "current_7d_pct": 0.0} for n in accounts},
            "slots": {},
            "swap_history": [],
        }))
        self.config_yaml = self.accounts_dir / "config.yaml"
        cus.write_yaml(self.config_yaml, config if config is not None else {"mode": "per_session"})
        self.inbox_md = self.accounts_dir / "inbox.md"

        self._saved = {k: getattr(cus, k) for k in (
            "HOME", "CLAUDE_DIR", "CREDS_JSON", "CLAUDE_JSON", "ACCOUNTS_DIR",
            "STATE_JSON", "CONFIG_YAML", "INBOX_MD", "DAEMON_PID", "migrate_account_dir")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CREDS_JSON = self.creds_json
        cus.CLAUDE_JSON = self.claude_json
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.state_json
        cus.CONFIG_YAML = self.config_yaml
        cus.INBOX_MD = self.inbox_md
        cus.DAEMON_PID = self.accounts_dir / "daemon.pid"
        cus.migrate_account_dir = lambda d: {"action": "already_migrated"}

        self._saved_mount_pids = cus.mount_pids
        self.live_slots: set[str] = set()
        cus.mount_pids = lambda mount: [1] if Path(mount).name in self.live_slots else []
        self._reset_caches()

        self.echoes: list[str] = []
        self._saved_echo = cus.click.echo
        cus.click.echo = lambda *a, **k: self.echoes.append(
            " ".join(str(x) for x in a) if a else "")
        self._patches: list[tuple[object, str, object]] = []

    @staticmethod
    def _reset_caches() -> None:
        cus._OCCUPIED_SLOTS_CACHE.clear()
        cus._SNAPSHOT_DEAD_PROBE.clear()
        cus._STORE_DEAD_PROBE.clear()
        cus._LANE_HEAL_HISTORY.clear()
        cus._LANE_TRANSFER_ATTEMPT.clear()
        cus._reset_blank_tracking()

    def patch(self, obj: object, name: str, value: object) -> None:
        self._patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def make_slot(self, account: str | None, live: bool, mount_creds: dict | None,
                  family_id: str | None = None, identity_of: str | None = None) -> str:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        if mount_creds is not None:
            (d / ".credentials.json").write_text(json.dumps(mount_creds))
        (d / ".claude.json").write_text(json.dumps(_identity(identity_of or account or "empty")))
        state["slots"][name]["account"] = account
        if family_id:
            state["slots"][name]["login_family"] = f"{account}/{family_id}"
        cus.save_state(state)
        if live:
            self.live_slots.add(name)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        return name

    def plant_family(self, account: str, family_id: str, creds: dict,
                     minted_days_ago: float | None = None) -> None:
        d = cus.login_family_dir(account, family_id)
        d.mkdir(parents=True, exist_ok=True)
        cus.login_family_creds_path(account, family_id).write_text(json.dumps(creds))
        if minted_days_ago is not None:
            minted = datetime.now(timezone.utc) - timedelta(days=minted_days_ago)
            cus.write_json(cus.login_family_provenance_path(account, family_id), {
                "account": account, "family_id": family_id,
                "minted_ts": minted.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "source_email": f"{account}@x", "refresh_fp": "sha256:test",
            })

    def slot_creds(self, slot: str) -> dict | None:
        p = cus.slot_path(slot) / ".credentials.json"
        return json.loads(p.read_text()) if p.exists() else None

    def slot_identity_email(self, slot: str) -> str | None:
        cj = json.loads((cus.slot_path(slot) / ".claude.json").read_text())
        return (cj.get("oauthAccount") or {}).get("emailAddress")

    def snap_creds(self, name: str) -> dict:
        return json.loads((self.accounts_dir / f"account-{name}" / ".credentials.json").read_text())

    def journal_path(self) -> Path:
        return self.accounts_dir / "swap.journal"

    def state(self) -> dict:
        return json.loads(self.state_json.read_text())

    def audit_lines(self, op: str) -> list[str]:
        return [e for e in self.echoes if e.startswith(f"{cus.CRED_AUDIT_PREFIX} op={op}")]

    def restore(self) -> None:
        for obj, name, value in reversed(self._patches):
            setattr(obj, name, value)
        cus.click.echo = self._saved_echo
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        self._reset_caches()
        self._tmp.cleanup()


def _grant_map(mapping: dict[str, tuple]):
    """Fake `_oauth_refresh_grant(rt)`; unmapped tokens raise so a test fails
    loudly if something probes a token it must not touch (e.g. a live family)."""
    def _grant(rt):
        if rt not in mapping:
            raise AssertionError(f"unexpected refresh-grant probe of {rt!r}")
        return mapping[rt]
    return _grant


def _alive(access: str, refresh: str, expires_in: int = 3600):
    return ("alive", {"access_token": access, "refresh_token": refresh, "expires_in": expires_in})


_ILGATE = {"independent_logins": {"use_independent_logins": True}, "mode": "per_session"}


# ===========================================================================
# FIX 1 — crash recovery vs refused swaps
# ===========================================================================

def test_1a_refused_swap_leaves_no_journal_and_no_identity_drift():
    """Pool-exhausted refusal: account 'a' is live on slot-1 (its only family
    leased), so moving slot-2 onto 'a' must refuse. Pre-fix the journal AND the
    target identity were already written by then; post-fix the mount is
    bit-for-bit untouched and no journal survives for recovery to act on."""
    env = _Env({"a": _valid("at-a", "rt-a"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.plant_family("a", "family-1", _valid("at-f1", "rt-f1"))
        env.make_slot("a", live=True, mount_creds=_valid("at-f1", "rt-f1"), family_id="family-1")
        lane = env.make_slot("other", live=True, mount_creds=_valid("at-o", "rt-o"))
        # No grant may fire: refusal happens before any probe of 'a'.
        env.patch(cus, "_oauth_refresh_grant", _grant_map({}))
        try:
            cus.execute_swap("a", slot=lane)
            raise AssertionError("expected the pool-exhausted refusal")
        except RuntimeError as e:
            assert "pool exhausted" in str(e), e
        assert not env.journal_path().exists(), "a REFUSED swap must not leave a journal"
        assert env.slot_identity_email(lane) == "other@x", "live identity must not drift to the target"
        assert cus._credential_refresh_token(env.slot_creds(lane)) == "rt-o"
        assert env.state()["slots"][lane]["account"] == "other"
        # Recovery on the next cycle is a pure no-op (nothing to reconcile).
        with cus._swap_lock():
            cus._recover_pending_swap()
        assert cus._credential_refresh_token(env.slot_creds(lane)) == "rt-o"
    finally:
        env.restore()


def test_1b_recovery_refuses_install_when_target_live_on_another_mount():
    """Reproduce the pre-fix on-disk shape exactly (journal + target identity
    stamped + outgoing tokens) and prove recovery now REFUSES: 'a' is live on
    slot-1, so a raw snapshot copy onto slot-2 would double-book its family
    (#104). Identity is rolled back, tokens untouched, evidence preserved."""
    env = _Env({"a": _valid("at-a", "rt-a"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.plant_family("a", "family-1", _valid("at-f1", "rt-f1"))
        env.make_slot("a", live=True, mount_creds=_valid("at-f1", "rt-f1"), family_id="family-1")
        lane = env.make_slot("other", live=True, mount_creds=_valid("at-o", "rt-o"), identity_of="a")
        cus.write_json(env.journal_path(), {"from": "other", "to": "a", "slot": lane, "ts": cus.now_iso()})
        env.patch(cus, "_oauth_refresh_grant", _grant_map({}))
        with cus._swap_lock():
            cus._recover_pending_swap()
        assert cus._credential_refresh_token(env.slot_creds(lane)) == "rt-o", "tokens must be untouched"
        assert env.slot_identity_email(lane) == "other@x", "identity must be rolled back to the outgoing account"
        assert env.state()["slots"][lane]["account"] == "other"
        assert not env.journal_path().exists()
        refused = list(env.accounts_dir.glob("swap.journal.refused.*"))
        assert len(refused) == 1 and json.loads(refused[0].read_text())["to"] == "a"
        assert any("decision=refused-install" in l for l in env.audit_lines("crash-recovery")), env.echoes
        assert "REFUSED" in env.inbox_md.read_text()
    finally:
        env.restore()


def test_1c_recovery_refuses_dead_target_snapshot():
    """Target snapshot is well-shaped-but-DEAD (refresh grant invalid_grant): the
    old raw copy would have installed it and blanked the lane on first refresh."""
    env = _Env({"merkos": _expired("rt-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        lane = env.make_slot("other", live=True, mount_creds=_valid("at-o", "rt-o"), identity_of="merkos")
        cus.write_json(env.journal_path(), {"from": "other", "to": "merkos", "slot": lane, "ts": cus.now_iso()})
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-dead": ("dead", None)}))
        with cus._swap_lock():
            cus._recover_pending_swap()
        assert cus._credential_refresh_token(env.slot_creds(lane)) == "rt-o"
        assert env.slot_identity_email(lane) == "other@x"
        assert env.state()["slots"][lane]["account"] == "other"
        assert list(env.accounts_dir.glob("swap.journal.refused.*"))
        refusal = env.audit_lines("crash-recovery")
        assert refusal and "invalid_grant" in refusal[0], env.echoes
    finally:
        env.restore()


def test_1d_recovery_still_completes_a_healthy_install():
    """Regression guard: a genuine crash between the identity write and the creds
    copy, with a healthy, un-held target, must still be finished by recovery."""
    env = _Env({"b": _valid("at-b", "rt-b"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        lane = env.make_slot("other", live=True, mount_creds=_valid("at-o", "rt-o"), identity_of="b")
        cus.write_json(env.journal_path(), {"from": "other", "to": "b", "slot": lane, "ts": cus.now_iso()})
        env.patch(cus, "_oauth_refresh_grant", _grant_map({}))  # valid access → no probe
        with cus._swap_lock():
            cus._recover_pending_swap()
        assert cus._credential_refresh_token(env.slot_creds(lane)) == "rt-b"
        assert env.state()["slots"][lane]["account"] == "b"
        assert not env.journal_path().exists()
        assert not list(env.accounts_dir.glob("swap.journal.refused.*"))
    finally:
        env.restore()


def test_1e_shared_mount_recovery_refuses_dead_target():
    """slot=None path: live ~/.claude identity says 'b' (crash window), creds are
    'a's, and 'b's snapshot is dead → refuse; state.active stays 'a', identity
    rolled back to 'a'."""
    env = _Env({"a": _valid("at-a", "rt-a"), "b": _expired("rt-b-dead")},
               active="a", config={"mode": "global"}, live_identity_of="b")
    try:
        cus.write_json(env.journal_path(), {"from": "a", "to": "b", "ts": cus.now_iso()})
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-b-dead": ("dead", None)}))
        with cus._swap_lock():
            cus._recover_pending_swap()
        assert env.state()["active"] == "a"
        assert json.loads(env.creds_json.read_text())["claudeAiOauth"]["refreshToken"] == "rt-a"
        assert json.loads(env.claude_json.read_text())["oauthAccount"]["emailAddress"] == "a@x"
        assert list(env.accounts_dir.glob("swap.journal.refused.*"))
    finally:
        env.restore()


def test_1f_live_mount_creds_invalid_expired_without_refresh():
    """Disk-only widening: expired access + NO refresh token is unusable (can't
    authenticate, can't mint). Every other shape keeps its previous verdict."""
    now_ms = int(time.time() * 1000)
    assert cus._live_mount_creds_invalid(
        {"claudeAiOauth": {"accessToken": "at", "refreshToken": "", "expiresAt": now_ms - 60_000}})
    assert cus._live_mount_creds_invalid(
        {"claudeAiOauth": {"accessToken": "at", "expiresAt": now_ms - 60_000}})
    # expired WITH a refresh token: still valid-shaped (the session self-refreshes;
    # dead-vs-alive is the probe's call, never this predicate's).
    assert not cus._live_mount_creds_invalid(_expired("rt-x"))
    # unexpired without a refresh token: authenticates right now → valid.
    assert not cus._live_mount_creds_invalid(
        {"claudeAiOauth": {"accessToken": "at", "expiresAt": now_ms + 3_600_000}})
    assert cus._live_mount_creds_invalid(_blank())
    assert not cus._live_mount_creds_invalid(_valid("at", "rt"))


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
