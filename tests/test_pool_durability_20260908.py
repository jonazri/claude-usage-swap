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
        assert any("decision=refused-install" in line for line in env.audit_lines("crash-recovery")), env.echoes
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


# ===========================================================================
# FIX 2 — generation-transfer heal for a pooled lane with a dead family
# ===========================================================================

def _pooled_dead_lane(env: _Env, account: str = "acct") -> str:
    """A LIVE pooled lane on `account` leasing family-1, whose mount is BLANK
    and whose family store is well-shaped-but-expired with refresh 'rt-f1-dead'.
    The account canonical is minted by the caller."""
    env.plant_family(account, "family-1", _expired("rt-f1-dead"), minted_days_ago=31)
    return env.make_slot(account, live=True, mount_creds=_blank(), family_id="family-1")


def test_2a_dead_family_alive_canonical_transfers_and_releases():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        lane = _pooled_dead_lane(env)
        # family-1's grant is DEAD; the canonical's is ALIVE and rotates.
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-f1-dead": ("dead", None),
                              "rt-canon": _alive("at-new", "rt-new")}))
        state = cus.load_state()
        healed = cus._auto_heal_live_lanes(state, cus.load_config())
        assert healed == [lane], env.echoes
        mount = env.slot_creds(lane)
        assert not cus._live_mount_creds_invalid(mount)
        assert cus._credential_refresh_token(mount) == "rt-new", mount
        st = env.state()
        assert st["slots"][lane]["login_family"] == "acct/family-2", st["slots"][lane]
        # New family holds the transferred generation + provenance; old one retired.
        assert cus._credential_refresh_token(
            json.loads(cus.login_family_creds_path("acct", "family-2").read_text())) == "rt-new"
        prov = json.loads(cus.login_family_provenance_path("acct", "family-2").read_text())
        assert "reseeded-from-canonical" in prov["note"]
        assert not cus.login_family_creds_path("acct", "family-1").exists()
        assert list(cus.login_family_dir("acct", "family-1").glob(".credentials.json.dead-*"))
        # The canonical is untouched by the transfer (it now holds a dead branch by
        # design — the documented reseed contract), never blanked.
        assert cus._credential_refresh_token(env.snap_creds("acct")) == "rt-canon"
        lines = env.audit_lines("lane-generation-transfer")
        assert lines and "decision=reseeded-and-released" in lines[-1], env.echoes
        assert "rt-new" not in lines[-1] and "rt-canon" not in lines[-1], "raw token leaked"
        assert "generation transfer" in env.inbox_md.read_text()
    finally:
        env.restore()


def test_2b_refuses_when_canonical_family_is_live_on_shared_mount():
    """The #104 guard that makes default-ON safe: the canonical's token is what
    the shared ~/.claude mount runs on → never consume it. No probe of
    'rt-canon' may fire (the grant map would raise)."""
    env = _Env({"acct": _valid("at-canon", "rt-canon")}, active="acct", config=_ILGATE)
    try:
        lane = _pooled_dead_lane(env)
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-f1-dead": ("dead", None)}))
        state = cus.load_state()
        assert cus._lane_heal_source(lane, "acct", state, cus.load_config()) is None
        lines = env.audit_lines("lane-generation-transfer")
        assert lines and "decision=refused-canonical-live-elsewhere" in lines[-1], env.echoes
        assert env.state()["slots"][lane]["login_family"] == "acct/family-1"
        assert cus.login_family_creds_path("acct", "family-1").exists(), "dead lease left in place"
    finally:
        env.restore()


def test_2c_gate_off_keeps_pre_fix_behavior():
    cfg = {"independent_logins": {"use_independent_logins": True, "heal_from_canonical": False},
           "mode": "per_session"}
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=cfg)
    try:
        lane = _pooled_dead_lane(env)
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-f1-dead": ("dead", None)}))
        assert cus._lane_heal_source(lane, "acct", cus.load_state(), cus.load_config()) is None
        assert not env.audit_lines("lane-generation-transfer")
        assert not cus.login_family_dir("acct", "family-2").exists()
    finally:
        env.restore()


def test_2d_alive_but_expired_family_is_refreshed_in_place_no_transfer():
    """A merely-expired family whose grant is ALIVE is the heal source (rotated
    tokens persisted into the store first) — no transfer, lease unchanged."""
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.plant_family("acct", "family-1", _expired("rt-f1"))
        lane = env.make_slot("acct", live=True, mount_creds=_blank(), family_id="family-1")
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-f1": _alive("at-f1n", "rt-f1n")}))
        healed = cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        assert healed == [lane], env.echoes
        assert cus._credential_refresh_token(env.slot_creds(lane)) == "rt-f1n"
        assert env.state()["slots"][lane]["login_family"] == "acct/family-1"
        assert not env.audit_lines("lane-generation-transfer")
    finally:
        env.restore()


def test_2e_dead_canonical_refuses_and_escalates():
    env = _Env({"acct": _expired("rt-canon-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        lane = _pooled_dead_lane(env)
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-f1-dead": ("dead", None), "rt-canon-dead": ("dead", None)}))
        assert cus._lane_heal_source(lane, "acct", cus.load_state(), cus.load_config()) is None
        lines = env.audit_lines("lane-generation-transfer")
        assert lines and "decision=refused-canonical-dead" in lines[-1], env.echoes
        # The lane is left for the URGENT relogin SOS.
        conds = cus.diagnose(cus.load_state(), cus.load_config())
        assert any(c.severity == "urgent" and lane in c.summary for c in conds), [c.summary for c in conds]
    finally:
        env.restore()


def test_2f_transfer_attempts_are_cooldown_bounded():
    env = _Env({"acct": _expired("rt-canon-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        lane = _pooled_dead_lane(env)
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-f1-dead": ("dead", None), "rt-canon-dead": ("dead", None)}))
        state, config = cus.load_state(), cus.load_config()
        assert cus._lane_heal_source(lane, "acct", state, config) is None
        n = len(env.audit_lines("lane-generation-transfer"))
        # Second call inside the cooldown: no new attempt (and no new probe).
        env.patch(cus, "_oauth_refresh_grant", _grant_map({}))
        cus._STORE_DEAD_PROBE.clear()  # family verdict re-evaluated → dead (cached in map? no: cleared)
        env.patch(cus, "_store_creds_dead", lambda *a, **k: True)
        assert cus._lane_heal_source(lane, "acct", state, config) is None
        assert len(env.audit_lines("lane-generation-transfer")) == n
    finally:
        env.restore()


# ===========================================================================
# FIX 3 — hygiene / visibility
# ===========================================================================

def test_3a_free_family_count_is_alive_free():
    env = _Env({"acct": _valid("at-canon", "rt-canon")}, active="acct", config=_ILGATE)
    try:
        env.plant_family("acct", "family-1", _valid("at-1", "rt-1"), minted_days_ago=31)   # past wall
        env.plant_family("acct", "family-2", _valid("at-2", "rt-2"), minted_days_ago=3)    # fresh
        env.plant_family("acct", "family-3", {"claudeAiOauth": {"accessToken": "at-3", "refreshToken": "rt-3",
                                                                "expiresAt": _PAST}}, minted_days_ago=3)
        state, config = cus.load_state(), cus.load_config()
        # family-3: expired access WITH a refresh token is merely suspect (a claim
        # probe decides) → still counted; family-1 is past the wall → not free.
        assert cus._free_family_count("acct", state, config) == 2
        assert cus.free_login_family("acct", state, config) == "family-2"
        # A probe-proven-dead verdict (cached the way prune/heal cache it) drops it.
        cus._STORE_DEAD_PROBE["fam:acct/family-3"] = (time.time(), True)
        assert cus._free_family_count("acct", state, config) == 1
        # Flag off: age is ignored again (disk-shape + probe cache still apply).
        cfg_off = json.loads(json.dumps(config))
        cfg_off["independent_logins"]["free_count_excludes_past_wall"] = False
        assert cus._free_family_count("acct", state, cfg_off) == 2
        assert cus.has_free_login_family("acct", state, config)
    finally:
        env.restore()


def _wall_conditions(conds):
    return [c for c in conds if "refresh-token wall" in c.summary]


def test_3b_family_wall_sos_is_urgent_for_live_lane_account_and_de_noised():
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "idle": _valid("at-i", "rt-i")},
               active="acct", config=_ILGATE)
    try:
        env.plant_family("acct", "family-1", _valid("at-1", "rt-1"), minted_days_ago=26)   # within 5d
        env.plant_family("acct", "family-2", _valid("at-2", "rt-2"), minted_days_ago=31)   # past
        env.plant_family("acct", "family-3", _valid("at-3", "rt-3"), minted_days_ago=2)    # fine
        env.plant_family("idle", "family-1", _valid("at-i1", "rt-i1"), minted_days_ago=29)
        env.make_slot("acct", live=True, mount_creds=_valid("at-1", "rt-1"), family_id="family-1")
        conds = cus.diagnose(cus.load_state(), cus.load_config())
        walls = {c.affected: c for c in _wall_conditions(conds)}
        assert walls["acct"].severity == "urgent", walls["acct"]
        assert "family-1" in walls["acct"].summary and "family-2" in walls["acct"].summary
        assert "family-3" not in walls["acct"].summary
        assert "LEASED by a live lane" in walls["acct"].action
        assert "cus login-mount acct" in walls["acct"].action
        # 'idle' backs nothing live → a warning, not an alarm.
        assert walls["idle"].severity == "warning"
        # De-noised: no legacy per-store "family-N->acct past assumed lifetime" line.
        assert not any("past assumed refresh-token lifetime" in c.summary for c in conds), \
            [c.summary for c in conds]
    finally:
        env.restore()


def test_3c_live_mount_creds_health_goes_urgent_near_the_wall():
    cfg = {"independent_logins": {"urgent_wall_within_days": 2, "warn_expiry_within_days": 5,
                                  "refresh_token_ttl_days": 30}}
    now_ms = int(time.time() * 1000)
    creds = _valid("at", "rt", expires_at=now_ms + 3_600_000)
    warn = cus._diagnose_mount_creds_health("slot-1", "acct", creds, now_ms, cfg, refresh_age_days=26.0)
    assert warn is not None and warn.severity == "warning", warn
    urgent = cus._diagnose_mount_creds_health("slot-1", "acct", creds, now_ms, cfg, refresh_age_days=28.5)
    assert urgent is not None and urgent.severity == "urgent", urgent
    past = cus._diagnose_mount_creds_health("slot-1", "acct", creds, now_ms, cfg, refresh_age_days=31.0)
    assert past is not None and past.severity == "urgent", past
    assert cus._diagnose_mount_creds_health("slot-1", "acct", creds, now_ms, cfg, refresh_age_days=10.0) is None


def test_3d_status_shows_per_family_age_and_wall():
    from click.testing import CliRunner
    env = _Env({"acct": _valid("at-canon", "rt-canon")}, active="acct", config=_ILGATE)
    try:
        env.plant_family("acct", "family-1", _valid("at-1", "rt-1"), minted_days_ago=31)
        env.plant_family("acct", "family-2", _valid("at-2", "rt-2"), minted_days_ago=3)
        # Restore real echo so CliRunner captures output.
        cus.click.echo = env._saved_echo
        res = CliRunner().invoke(cus.cli, ["status"])
        out = res.output
        assert res.exit_code == 0, out
        assert "2 family(ies), 1 free" in out and "1 free-but-unusable" in out, out
        assert "family-1" in out and "PAST WALL" in out, out
        assert "family-2" in out and "wall in 2" in out, out
        assert "age 31." in out and "age 3." in out, out
    finally:
        env.restore()


# ===========================================================================
# Dual-review fixes (PR #201): F-F-1 (no_execute), F-F-2 (lock/lost-update),
# F-F-3 (crash-after-claimed-copy lease restore), F-F-6 (heal-candidate #104)
# ===========================================================================

def test_ff1_no_execute_transfer_is_zero_writes():
    """F-F-1: `cus daemon --once --no-execute` must fire ZERO grants/writes. A
    pooled lane leasing a BLANK family (so the transfer path is reached without a
    probe) with an alive canonical: the dry-run logs a WOULD line and touches
    NOTHING — no new family, no grant (the grant map raises on ANY probe), lease +
    canonical + state.json all byte-identical."""
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        # BLANK family store → `_heal_candidate_usable` returns False WITHOUT a
        # probe, so `_lane_heal_source` reaches the generation transfer.
        env.plant_family("acct", "family-1", _blank(), minted_days_ago=31)
        lane = env.make_slot("acct", live=True, mount_creds=_blank(), family_id="family-1")
        # Any grant at all is a failure under --no-execute (the map raises).
        env.patch(cus, "_oauth_refresh_grant", _grant_map({}))
        state_before = env.state_json.read_text()
        healed = cus._auto_heal_live_lanes(cus.load_state(), cus.load_config(), no_execute=True)
        assert healed == [], env.echoes
        # ZERO writes: no family-2 minted, lease unchanged, canonical untouched,
        # state.json byte-identical, mount still blank.
        assert not cus.login_family_dir("acct", "family-2").exists()
        assert env.state()["slots"][lane]["login_family"] == "acct/family-1"
        assert cus._credential_refresh_token(env.snap_creds("acct")) == "rt-canon"
        assert env.state_json.read_text() == state_before
        assert cus._live_mount_creds_invalid(env.slot_creds(lane))
        # A WOULD line was logged; no transfer DECISION (refuse/reseed) was written.
        assert any("--no-execute" in e and "WOULD" in e and "transfer" in e for e in env.echoes), env.echoes
        assert not env.audit_lines("lane-generation-transfer")
        # The account is NOT flagged snapshot_refresh_dead in a dry run.
        assert not env.state()["accounts"]["acct"].get("snapshot_refresh_dead")
    finally:
        env.restore()


def test_ff2_transfer_uses_fresh_state_no_lost_update():
    """F-F-2: the transfer runs UNDER the swap lock on a FRESHLY loaded state and
    saves THAT — so a concurrent state change (a `cus slot move`/`switch` that
    committed during the SOS pass) is preserved, not clobbered by a save of the
    pass-start copy. Proven by mutating state.json after the caller's load and
    asserting BOTH the concurrent change AND the new lease survive."""
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        lane = _pooled_dead_lane(env)  # expired family-1 (dead grant) → transfer fires
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-f1-dead": ("dead", None), "rt-canon": _alive("at-new", "rt-new")}))
        stale = cus.load_state()  # the pass-start copy (current_5h_pct == 0.0)
        # A concurrent writer commits an UNRELATED change AFTER the stale load.
        concurrent = cus.load_state()
        concurrent["accounts"]["acct"]["current_5h_pct"] = 42.0
        cus.save_state(concurrent)
        # Heal with the STALE copy: the transfer must reload fresh state internally.
        healed = cus._auto_heal_live_lanes(stale, cus.load_config())
        assert healed == [lane], env.echoes
        final = env.state()
        # The concurrent change survived (transfer saved FRESH state, not the stale
        # copy which still read 0.0) AND the new lease was applied.
        assert final["accounts"]["acct"]["current_5h_pct"] == 42.0, final["accounts"]["acct"]
        assert final["slots"][lane]["login_family"] == "acct/family-2", final["slots"][lane]
        # F-F-5: the transfer flags the (now dead-branched) canonical.
        assert final["accounts"]["acct"].get("snapshot_refresh_dead") is True
    finally:
        env.restore()


def test_ff3_recovery_restores_claimed_family_lease_1g():
    """F-F-3 (test 1g): a crash AFTER the claimed-family creds copy but BEFORE
    save_state left the live lane running family-2 while state showed it FREE — the
    next probe would rotate its token (#104). Recovery now reads the journal's
    `family` and, when the live creds match that family, restores the lease."""
    env = _Env({"a": _valid("at-a", "rt-a"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.plant_family("a", "family-2", _valid("at-f2", "rt-f2"))
        # Live lane: mount already holds family-2's generation (copy done), identity
        # already stamped "a" (identity write done), but state.account still "other"
        # and NO login_family (save_state never ran) — the crash-after-copy window.
        lane = env.make_slot("other", live=True, mount_creds=_valid("at-f2", "rt-f2"), identity_of="a")
        cus.write_json(env.journal_path(),
                       {"from": "other", "to": "a", "slot": lane, "family": "family-2", "ts": cus.now_iso()})
        env.patch(cus, "_oauth_refresh_grant", _grant_map({}))  # classify + fp match probe nothing
        with cus._swap_lock():
            cus._recover_pending_swap()
        st = env.state()
        assert st["slots"][lane]["account"] == "a", st["slots"][lane]
        assert st["slots"][lane]["login_family"] == "a/family-2", st["slots"][lane]
        assert cus._credential_refresh_token(env.slot_creds(lane)) == "rt-f2", "mount tokens untouched"
        assert not env.journal_path().exists()
    finally:
        env.restore()


def test_ff3b_recovery_does_not_restore_lease_when_bytes_dont_match():
    """F-F-3 guard: never restore a lease the actual token bytes don't back. If the
    live mount holds a DIFFERENT generation than the journal's family, the lease is
    left unset (the crash landed before the family copy)."""
    env = _Env({"a": _valid("at-a", "rt-a"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.plant_family("a", "family-2", _valid("at-f2", "rt-f2"))
        # Mount holds "a"'s SNAPSHOT generation (rt-a), not family-2's — a crash
        # before the family copy. Identity stamped "a".
        lane = env.make_slot("other", live=True, mount_creds=_valid("at-a", "rt-a"), identity_of="a")
        cus.write_json(env.journal_path(),
                       {"from": "other", "to": "a", "slot": lane, "family": "family-2", "ts": cus.now_iso()})
        env.patch(cus, "_oauth_refresh_grant", _grant_map({}))
        with cus._swap_lock():
            cus._recover_pending_swap()
        st = env.state()
        assert st["slots"][lane]["account"] == "a"
        assert "login_family" not in st["slots"][lane], st["slots"][lane]
    finally:
        env.restore()


def test_ff6_heal_candidate_refuses_family_live_on_another_mount():
    """F-F-6: a heal candidate whose refresh family is LIVE on another mount is the
    #104 double-book — reinstalling it would copy a running session's single-use
    token onto a 2nd live mount. `_heal_candidate_usable` returns False (was: True)
    and logs the refusal; no probe fires (the grant map would raise)."""
    env = _Env({"acct": _valid("at-canon", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.plant_family("acct", "family-1", _valid("at-x", "rt-x"))
        # slot-1 is LIVE on acct running rt-x (family-1's generation).
        env.make_slot("acct", live=True, mount_creds=_valid("at-x", "rt-x"), family_id="family-1")
        env.patch(cus, "_oauth_refresh_grant", _grant_map({}))  # any probe is a failure
        state, config = cus.load_state(), cus.load_config()
        usable = cus._heal_candidate_usable(
            cus.login_family_creds_path("acct", "family-1"), "heal-fam:acct/family-1",
            "acct", state, config)
        assert usable is False, env.echoes
        lines = env.audit_lines("heal-candidate")
        assert lines and "decision=refused-live-elsewhere" in lines[-1], env.echoes
    finally:
        env.restore()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
