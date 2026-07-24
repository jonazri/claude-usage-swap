"""Shared-family fail-closed guard (GH #15, 2026-07-24 revocation incident).

With per_session.lane_sharing on, two live mounts ended up on one account
WITHOUT independent login families: both refreshed the SAME OAuth
refresh-token family, rotation on one invalidated the other, and re-presenting
the rotated-away token tripped the auth server's REUSE DETECTION — which
revoked the whole family server-side and took the sentinel down.

The daemon already DETECTED the condition ("[URGENT] <account> is live on 2
mounts without independent logins") but only logged it: every execute-time
double-book refusal in _execute_swap_locked was gated on
independent_logins_enabled(config), so with the gate OFF (the default) a
shared-snapshot COPY installed onto a second live mount with no refusal at
all — swap_install_source's deliberate "lazy fallback". _slot_move_plan even
previewed "refuse" for exactly this case while execution proceeded.

These tests pin the fix: making an account live on a SECOND mount without a
distinct independent family must REFUSE (fail closed) regardless of the
independent_logins gate, with an error naming `cus login-mount <account>`;
`independent_logins.allow_shared_family: true` is the conscious opt-back-in
(old behavior, URGENT detection preserved).

Committee finding 1 (2026-07-24) — byte-level, name-AGNOSTIC layer: the
name-keyed guard resolves state['slots'][s]['account'] LABELS, so two
DIFFERENTLY-NAMED accounts carrying ONE refresh-token family (a copied/renamed
account dir, or two logical accounts logged into the same Anthropic login)
each passed under their own name. The `test_cross_name_*` /
`test_candidate_matching_shared_mount_*` tests pin the byte-level guard that
closes this: candidate install bytes fingerprint-matched against EVERY other
live mount regardless of account name, hatch honored gate-off only.

Committee ROUND 2 (2026-07-24): `test_refused_swap_leaves_no_residue_*` /
`test_late_install_gate_refusal_unwinds_*` pin the no-residue invariant — a
REFUSED swap must leave no swap journal and no merged identity behind, else
the next swap's _recover_pending_swap "completes" the refused install as
crash recovery (guard-free roll-forward — the round-2 critical finding).
`test_slot_move_preview_names_cross_name_byte_collision` pins the preview's
byte-level overlay (finding 2); the slot=None / per_session tests close the
coverage gaps of finding 4.

Committee ROUND 3 (2026-07-24): the `test_recovery_*` trio pins that crash
recovery honors the guards — the journal records the APPROVED install source
(a claimed family recovers as FAMILY bytes, never the snapshot; legacy
journals keep GH #76 snapshot semantics) and the roll-forward copy runs the
same byte-level collision check (refuse ⇒ no copy + journal cleared + audit).
`test_refusal_unwind_restore_failure_still_clears_journal` pins the
journal-cleared-FIRST unwind ordering (finding 2);
`test_gate_on_shared_mount_hatch_byte_identical_still_refused` pins the
gate-ON hatch boundary the docs now state precisely (finding 3).

Committee ROUND 4 (2026-07-24):
`test_recovery_roll_forward_blank_source_skips_copy` pins that the recovery
roll-forward also honors the GH #141 blank/unreadable gate —
_candidate_family_colliders returns (None, []) for those sources and
delegates the refusal to a gate that lives only in _execute_swap_locked,
which recovery never re-enters; a crash in the journal-write → late-gate
window (the save-back section is real I/O) journals an UNVALIDATED
install_src, and pre-fix recovery installed its blank bytes onto a live mount.

Run standalone:  python3 tests/test_shared_family_guard.py
Run under pytest: pytest tests/test_shared_family_guard.py
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


class _Env:
    """Throwaway on-disk tree — same monkeypatch pattern as test_login_pool."""

    def __init__(self, accounts=("alpha", "beta")) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        (self.claude_dir / ".credentials.json").write_text(json.dumps(_creds("rt-bare")))
        # Shared-mount live .claude.json — required for a slot=None (global
        # `cus switch`) swap: an occupied shared mount with no live .claude.json
        # refuses before the guards under test are even reached.
        (root / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": f"{accounts[0]}@x"}}))
        for name in accounts:
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{name}")))
            (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": f"{name}@x"}}))
        cus.write_json(self.accounts_dir / "state.json", {
            "active": accounts[0],
            "accounts": {n: {"next_swap_at_pct": 50, "current_5h_pct": 0.0, "current_7d_pct": 0.0} for n in accounts},
            "slots": {},
            "swap_history": [],
        })
        self._saved = {k: getattr(cus, k) for k in
                       ("HOME", "CLAUDE_DIR", "CLAUDE_JSON", "CREDS_JSON", "ACCOUNTS_DIR",
                        "STATE_JSON", "CONFIG_YAML")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CLAUDE_JSON = root / ".claude.json"
        cus.CREDS_JSON = self.claude_dir / ".credentials.json"
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.accounts_dir / "state.json"
        cus.CONFIG_YAML = self.accounts_dir / "config.yaml"

        self._saved_mount_pids = cus.mount_pids
        self.live_slots: set[str] = set()
        cus.mount_pids = lambda mount: [1] if Path(mount).name in self.live_slots else []
        # The fake holder must read as a live claude SESSION — the guard under
        # test is session-aware (orphan-holds-slot bug, 2026-07-10), so a holder
        # with a non-claude comm would look like an orphan and the account would
        # (correctly) not count as held.
        self._saved_pid_comm = cus._pid_comm
        cus._pid_comm = lambda pid: "claude"
        cus._OCCUPIED_SLOTS_CACHE.clear()

        # #127: never hit the real OAuth endpoint. "unknown" = fail open, which
        # is byte-identical to pre-#127 claim behavior.
        self._saved_probe = cus._oauth_refresh_grant
        cus._oauth_refresh_grant = lambda rt: ("unknown", None)

        # click.echo capture (CRED-AUDIT / URGENT assertions) — same pattern as
        # test_dead_snapshot_family_seed's harness.
        self.echoes: list[str] = []
        self._saved_echo = cus.click.echo
        cus.click.echo = lambda *a, **k: self.echoes.append(
            " ".join(str(x) for x in a) if a else "")

    def set_config(self, cfg: dict) -> None:
        cus.write_yaml(cus.CONFIG_YAML, cfg)

    def plant_family(self, account: str, family_id: str, refresh: str) -> None:
        d = cus.login_family_dir(account, family_id)
        d.mkdir(parents=True, exist_ok=True)
        cus.login_family_creds_path(account, family_id).write_text(json.dumps(_creds(refresh)))

    def make_slot(self, account: str, live: bool) -> str:
        """Create a slot holding `account` with the account's SNAPSHOT family in
        its mount (a plain copy — matching a slot that swapped in via the copy
        path, i.e. exactly the shared-family second-mount precondition)."""
        state = cus.load_state()
        name, d = cus.create_slot(state)
        (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{account}")))
        (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": f"{account}@x"}}))
        state["slots"][name]["account"] = account
        cus.save_state(state)
        if live:
            self.live_slots.add(name)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        return name

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus._pid_comm = self._saved_pid_comm
        cus._oauth_refresh_grant = self._saved_probe
        cus.click.echo = self._saved_echo
        cus._OCCUPIED_SLOTS_CACHE.clear()
        self._tmp.cleanup()


def test_gate_off_second_mount_refuses_shared_family_copy():
    """DEFAULT config (independent_logins gate OFF, no escape hatch): a swap
    that would make an account live on a SECOND mount must RAISE — the install
    source is a shared-family snapshot copy, the exact GH #15 precondition —
    and the error must name the remedy (`cus login-mount <account>`).
    Pre-fix this installed the clobbering copy silently (the daemon's URGENT
    line was the only trace) because every execute-time double-book refusal
    was gated on independent_logins_enabled()."""
    env = _Env()
    try:
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        cus.execute_swap("beta", trigger="auto-ladder", slot=s1)  # beta unheld: legal first mount
        raised_msg = ""
        try:
            cus.execute_swap("beta", trigger="auto-ladder", slot=s2)
        except RuntimeError as e:
            raised_msg = str(e)
        assert raised_msg, "second live mount on beta's shared family must refuse, not clobber (GH #15)"
        assert "cus login-mount beta" in raised_msg, raised_msg
        assert "allow_shared_family" in raised_msg, raised_msg
        # The name-keyed refusal leaves its CRED-AUDIT trace (round 3 finding 6).
        assert any("op=shared-family-refuse" in e for e in env.echoes), env.echoes
        # The refused lane held: still on alpha, live creds untouched.
        assert cus.load_state()["slots"][s2]["account"] == "alpha"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(s2) / ".credentials.json")) == "rt-alpha"
    finally:
        env.restore()


def test_gate_off_first_mount_snapshot_copy_still_installs():
    """The guard must NOT over-fire: an account NOT live anywhere installs via
    the plain snapshot copy exactly as before (the everyday gate-off swap)."""
    env = _Env()
    try:
        mover = env.make_slot("alpha", live=True)
        cus.execute_swap("beta", trigger="auto-ladder", slot=mover)
        assert cus.load_state()["slots"][mover]["account"] == "beta"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(mover) / ".credentials.json")) == "rt-beta"
    finally:
        env.restore()


def test_second_mount_with_free_family_still_leases():
    """Gate ON + a FREE pooled family (allow_shared_family left at its False
    default): the second mount claims the DISTINCT family and records the
    lease — the supported GH #109 rescue is untouched by the new guard."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)              # beta already held
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        cus.execute_swap("beta", trigger="auto-ladder", slot=mover)
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(mover) / ".credentials.json")) == "rt-beta-fam1"
        assert cus.load_state()["slots"][mover]["login_family"] == "beta/family-1"
    finally:
        env.restore()


def test_escape_hatch_restores_old_behavior_and_urgent_detection_stays():
    """independent_logins.allow_shared_family: true — the conscious operator
    opt-in — restores the pre-fix behavior (the shared-family copy installs,
    double-booking the account) AND the daemon's URGENT detection still fires,
    so the opted-in operator keeps the warning the incident relied on."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"allow_shared_family": True}})
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        cus.execute_swap("beta", trigger="auto-ladder", slot=s1)
        cus.execute_swap("beta", trigger="auto-ladder", slot=s2)  # opted in: proceeds
        assert cus.load_state()["slots"][s2]["account"] == "beta"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(s2) / ".credentials.json")) == "rt-beta"
        # The URGENT's backing detector still sees the double-book...
        db = cus.double_booked_live_accounts(cus.load_state())
        assert any(d["account"] == "beta" and len(d["mounts"]) == 2 for d in db), db
        # ...and diagnose still surfaces it as an URGENT condition.
        conds = cus.diagnose(cus.load_state(), cus.load_config())
        assert any("without independent logins" in c.summary and c.severity == "urgent"
                   for c in conds), [c.summary for c in conds]
    finally:
        env.restore()


def test_slot_move_preview_agrees_with_escape_hatch():
    """_slot_move_plan's contract is that the preview NEVER diverges from what
    execute_swap really does. Pre-fix it previewed "refuse" for the gate-off
    double-book while execution happily installed the copy; post-fix the
    default previews (and executes) refuse, and the hatch previews (and
    executes) the shared copy."""
    env = _Env()
    try:
        env.make_slot("beta", live=True)              # beta held elsewhere
        mover = env.make_slot("alpha", live=True)
        # Default: refuse — and say how to fix it.
        plan = cus._slot_move_plan(cus.load_state(), cus.load_config(), mover, "beta")
        assert plan["plan"] == "refuse", plan
        assert "login-mount beta" in plan["detail"], plan
        # Hatch on: the old shared-copy behavior, named for what it is.
        env.set_config({"independent_logins": {"allow_shared_family": True}})
        plan2 = cus._slot_move_plan(cus.load_state(), cus.load_config(), mover, "beta")
        assert plan2["plan"] == "snapshot", plan2
        assert "allow_shared_family" in plan2["detail"], plan2
    finally:
        env.restore()


def test_cross_name_shared_family_second_mount_refuses():
    """FINDING 1 (committee, 2026-07-24): 'gamma' is a RENAMED COPY of 'beta'
    — same OAuth refresh-token family, different logical name. Beta's family is
    live on a lane; installing gamma onto another lane is a second live mount
    on that SAME family, but every name-keyed guard sees gamma as held nowhere.
    The byte-level name-agnostic guard must refuse, naming the fingerprint."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        (env.accounts_dir / "account-gamma" / ".credentials.json").write_text(
            json.dumps(_creds("rt-beta")))
        env.make_slot("beta", live=True)              # beta's family live on a lane
        mover = env.make_slot("alpha", live=True)
        raised_msg = ""
        try:
            cus.execute_swap("gamma", trigger="auto-ladder", slot=mover)
        except RuntimeError as e:
            raised_msg = str(e)
        assert raised_msg, "cross-name shared family must refuse at the byte level (GH #15 finding 1)"
        assert cus._refresh_fingerprint("rt-beta") in raised_msg, raised_msg
        assert "cus login-mount gamma" in raised_msg, raised_msg
        assert any("op=family-collision-refuse" in e for e in env.echoes), env.echoes
        # The refused lane held: still on alpha, live creds untouched.
        assert cus.load_state()["slots"][mover]["account"] == "alpha"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(mover) / ".credentials.json")) == "rt-alpha"
    finally:
        env.restore()


def test_cross_name_shared_family_hatch_proceeds_with_urgent():
    """Gate off + allow_shared_family: the conscious opt-in keeps the OLD
    proceed contract for the cross-name collision too — but LOUDLY: an
    [URGENT] line plus a CRED-AUDIT record, so the opted-in operator keeps
    the warning the 2026-07-24 incident relied on."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        env.set_config({"independent_logins": {"allow_shared_family": True}})
        (env.accounts_dir / "account-gamma" / ".credentials.json").write_text(
            json.dumps(_creds("rt-beta")))
        env.make_slot("beta", live=True)
        mover = env.make_slot("alpha", live=True)
        cus.execute_swap("gamma", trigger="auto-ladder", slot=mover)   # opted in: proceeds
        assert cus.load_state()["slots"][mover]["account"] == "gamma"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(mover) / ".credentials.json")) == "rt-beta"
        assert any("[URGENT]" in e and "allow_shared_family" in e for e in env.echoes), env.echoes
        assert any("op=family-collision-hatch" in e for e in env.echoes), env.echoes
    finally:
        env.restore()


def test_cross_name_distinct_families_unaffected():
    """No byte overlap ⇒ the name-agnostic guard stays silent: an everyday
    swap onto an account whose own family is live nowhere else installs
    exactly as before, even with other accounts live on other lanes."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        env.make_slot("beta", live=True)
        mover = env.make_slot("alpha", live=True)
        cus.execute_swap("gamma", trigger="auto-ladder", slot=mover)
        assert cus.load_state()["slots"][mover]["account"] == "gamma"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(mover) / ".credentials.json")) == "rt-gamma"
        assert not any("op=family-collision-refuse" in e or "op=family-collision-hatch" in e
                       for e in env.echoes), env.echoes
    finally:
        env.restore()


def test_cross_name_collision_gate_on_refuses_hatch_or_not():
    """Gate ON: the operator asked for independent families, so the hatch is
    NOT consulted (finding 2's documented gate-off-only asymmetry) — a
    cross-name byte collision refuses even with allow_shared_family: true."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True,
                                               "allow_shared_family": True}})
        (env.accounts_dir / "account-gamma" / ".credentials.json").write_text(
            json.dumps(_creds("rt-beta")))
        env.make_slot("beta", live=True)
        mover = env.make_slot("alpha", live=True)
        raised_msg = ""
        try:
            cus.execute_swap("gamma", trigger="auto-ladder", slot=mover)
        except RuntimeError as e:
            raised_msg = str(e)
        assert raised_msg, "gate-on cross-name collision must refuse regardless of the hatch"
        assert cus._refresh_fingerprint("rt-beta") in raised_msg, raised_msg
        assert cus.load_state()["slots"][mover]["account"] == "alpha"
    finally:
        env.restore()


def test_candidate_matching_shared_mount_family_refuses():
    """The shared ~/.claude mount counts as a live holder UNCONDITIONALLY in
    global/hybrid (bare sessions set no CLAUDE_CONFIG_DIR, so mount_pids can't
    see them — the issue-#141 blind spot): a candidate whose family matches
    the shared mount's live bytes refuses even though no holder is detectable,
    and even though the target's NAME isn't what the shared mount runs."""
    env = _Env()
    try:
        # beta's snapshot is a copy of whatever the shared mount runs (rt-bare).
        (env.accounts_dir / "account-beta" / ".credentials.json").write_text(
            json.dumps(_creds("rt-bare")))
        mover = env.make_slot("alpha", live=True)
        raised_msg = ""
        try:
            cus.execute_swap("beta", trigger="auto-ladder", slot=mover)
        except RuntimeError as e:
            raised_msg = str(e)
        assert raised_msg, "candidate sharing the shared mount's live family must refuse"
        assert "shared-mount" in raised_msg, raised_msg
        assert cus._refresh_fingerprint("rt-bare") in raised_msg, raised_msg
        assert cus.load_state()["slots"][mover]["account"] == "alpha"
    finally:
        env.restore()


def test_shared_mount_destination_refuses_shared_family_copy():
    """slot=None — the SHARED ~/.claude mount as the swap DESTINATION (a global
    `cus switch`), previously never driven by these tests (committee round 2,
    finding 4): with beta live on a lane, switching the shared mount onto beta
    would double-book beta's family, and slot=None has no pool to claim from —
    the fail-closed guard must refuse and name the shared mount, leaving
    state.active and the live creds untouched."""
    env = _Env()
    try:
        env.make_slot("beta", live=True)              # beta live on a lane
        raised_msg = ""
        try:
            cus.execute_swap("beta", trigger="manual")
        except RuntimeError as e:
            raised_msg = str(e)
        assert raised_msg, "shared-mount destination double-book must refuse (GH #15)"
        assert "the shared mount" in raised_msg, raised_msg
        assert "cus login-mount beta" in raised_msg, raised_msg
        assert cus.load_state()["active"] == "alpha"
        assert cus._credential_refresh_token(cus.read_json(cus.CREDS_JSON)) == "rt-bare"
        assert not cus._swap_journal_path().exists()
    finally:
        env.restore()


def test_per_session_mode_second_lane_refuses_shared_family_copy():
    """mode: per_session — the lane-only mode the sentinel actually runs,
    previously never driven by these tests (committee round 2, finding 4). The
    guard's occupancy read is mode-aware (the shared mount is detectable-only
    in per_session), but a SECOND LANE on one account must refuse exactly as
    in global mode: the holder here is a live lane, not the shared mount."""
    env = _Env()
    try:
        env.set_config({"mode": "per_session"})
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        cus.execute_swap("beta", trigger="auto-ladder", slot=s1)   # first mount: legal
        raised_msg = ""
        try:
            cus.execute_swap("beta", trigger="auto-ladder", slot=s2)
        except RuntimeError as e:
            raised_msg = str(e)
        assert raised_msg, "per_session second lane on one family must refuse (GH #15)"
        assert cus.load_state()["slots"][s2]["account"] == "alpha"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(s2) / ".credentials.json")) == "rt-alpha"
    finally:
        env.restore()


def test_refused_swap_leaves_no_residue_name_keyed_guard():
    """Committee round 2, finding 1 (CRITICAL): pre-fix the GH #15 refusals
    fired AFTER _write_swap_journal + the live .claude.json identity merge, so
    a refusal left a journal + a merged TARGET identity behind — and the NEXT
    execute_swap's unconditional _recover_pending_swap read live==target with
    foreign creds and atomic_copy'd the REFUSED target's creds onto the mount
    as 'crash recovery', completing the exact clobber that was refused.
    Post-fix the guards run pre-journal/pre-merge: a refused swap leaves the
    world exactly as before the call. Shape: the name-keyed refusal."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        cus.execute_swap("beta", trigger="auto-ladder", slot=s1)
        try:
            cus.execute_swap("beta", trigger="auto-ladder", slot=s2)   # refused
        except RuntimeError:
            pass
        # No residue: no journal, live identity still the pre-swap occupant's.
        assert not cus._swap_journal_path().exists(), "refusal must not leave a swap journal"
        assert cus.read_json(cus.slot_path(s2) / ".claude.json")[
            "oauthAccount"]["emailAddress"] == "alpha@x"
        # A direct recovery pass finds nothing to roll forward...
        cus._recover_pending_swap()
        assert cus.load_state()["slots"][s2]["account"] == "alpha"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(s2) / ".credentials.json")) == "rt-alpha"
        # ...and the next real swap (unrelated, legit target) is unpoisoned —
        # pre-fix, THIS call's recovery step installed beta's refused creds.
        cus.execute_swap("gamma", trigger="auto-ladder", slot=s2)
        assert cus.load_state()["slots"][s2]["account"] == "gamma"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(s2) / ".credentials.json")) == "rt-gamma"
    finally:
        env.restore()


def test_refused_swap_leaves_no_residue_byte_level_guard():
    """Same invariant, driven through the byte-level name-AGNOSTIC refusal —
    the committee's exact trace shape (gamma = renamed copy of beta). After the
    refusal: journal clear, pre-swap identity intact, and a direct
    _recover_pending_swap() pass installs nothing."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        (env.accounts_dir / "account-gamma" / ".credentials.json").write_text(
            json.dumps(_creds("rt-beta")))
        env.make_slot("beta", live=True)
        mover = env.make_slot("alpha", live=True)
        try:
            cus.execute_swap("gamma", trigger="auto-ladder", slot=mover)   # refused
        except RuntimeError:
            pass
        assert not cus._swap_journal_path().exists(), "byte-level refusal must not leave a swap journal"
        assert cus.read_json(cus.slot_path(mover) / ".claude.json")[
            "oauthAccount"]["emailAddress"] == "alpha@x"
        cus._recover_pending_swap()
        assert cus.load_state()["slots"][mover]["account"] == "alpha"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(mover) / ".credentials.json")) == "rt-alpha"
    finally:
        env.restore()


def test_late_install_gate_refusal_unwinds_journal_and_identity():
    """The one refusal that legitimately remains PAST the journal write is the
    GH #141 definitive install-point gate (it must validate the bytes at THE
    write). Drive it via the gate-on pool path — a claimed family whose store
    is BLANK-shaped (claimable fail-open under an unverifiable probe, colliding
    with nothing) — and pin the refusal-unwind backstop: journal retired,
    pre-merge identity restored, creds untouched, recovery a no-op."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        env.make_slot("beta", live=True)              # beta held → pool-claim path
        mover = env.make_slot("alpha", live=True)
        d = cus.login_family_dir("beta", "family-1")
        d.mkdir(parents=True, exist_ok=True)
        cus.login_family_creds_path("beta", "family-1").write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "", "refreshToken": "rt-beta-fam1",
                               "expiresAt": 2_000_000_000_000}}))
        raised_msg = ""
        try:
            cus.execute_swap("beta", trigger="auto-ladder", slot=mover)
        except RuntimeError as e:
            raised_msg = str(e)
        assert "blank/expired" in raised_msg, raised_msg
        assert not cus._swap_journal_path().exists(), "late-gate refusal must retire the journal"
        assert cus.read_json(cus.slot_path(mover) / ".claude.json")[
            "oauthAccount"]["emailAddress"] == "alpha@x"
        cus._recover_pending_swap()
        assert cus.load_state()["slots"][mover]["account"] == "alpha"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(mover) / ".credentials.json")) == "rt-alpha"
    finally:
        env.restore()


def test_slot_move_preview_names_cross_name_byte_collision():
    """Committee round 2, finding 2: _slot_move_plan was name-keyed only — for
    a cross-name byte collision it previewed 'snapshot — installs cleanly'
    while execution refused, violating its own preview/reality contract.
    Post-fix the preview runs the same fingerprint comparison: default
    previews the refusal (naming the family + colliding mount), hatch-on
    (gate off) previews would-proceed-with-URGENT."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        (env.accounts_dir / "account-gamma" / ".credentials.json").write_text(
            json.dumps(_creds("rt-beta")))
        env.make_slot("beta", live=True)
        mover = env.make_slot("alpha", live=True)
        plan = cus._slot_move_plan(cus.load_state(), cus.load_config(), mover, "gamma")
        assert plan["plan"] == "refuse", plan
        assert cus._refresh_fingerprint("rt-beta") in plan["detail"], plan
        assert "login-mount gamma" in plan["detail"], plan
        # Hatch on (gate off): execution proceeds LOUDLY — preview must agree.
        env.set_config({"independent_logins": {"allow_shared_family": True}})
        plan2 = cus._slot_move_plan(cus.load_state(), cus.load_config(), mover, "gamma")
        assert plan2["plan"] == "snapshot", plan2
        assert "URGENT" in plan2["detail"], plan2
    finally:
        env.restore()


def test_recovery_completes_from_journaled_family_not_snapshot():
    """Committee ROUND 3, finding 1(i): a crash in the merge→copy window used
    to recover from the account SNAPSHOT even when the crashed swap's approved
    install source was a CLAIMED POOL FAMILY — double-booking the shared
    family the forward guards had just steered around. Post-fix the journal
    records the approved source and recovery copies THAT: the family bytes
    land, not the snapshot's, and the lease the crashed swap never got to
    persist is re-recorded (so the family can't be re-claimed elsewhere)."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        env.make_slot("beta", live=True)              # beta live → family, not snapshot
        mover = env.make_slot("alpha", live=True)
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        # Crash simulation: journal written (with the approved family source)
        # + identity merged; the creds copy never happened.
        cus._write_swap_journal("alpha", "beta", "auto-ladder", slot=mover,
                                install_src=cus.login_family_creds_path("beta", "family-1"),
                                used_independent=True, login_family="beta/family-1")
        (cus.slot_path(mover) / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "beta@x"}}))
        cus._recover_pending_swap()
        assert not cus._swap_journal_path().exists()
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(mover) / ".credentials.json")) == "rt-beta-fam1", \
            "recovery must install the journaled FAMILY bytes, never the snapshot"
        st = cus.load_state()
        assert st["slots"][mover]["account"] == "beta"
        assert st["slots"][mover]["login_family"] == "beta/family-1"
    finally:
        env.restore()


def test_recovery_roll_forward_collision_refuses_no_copy():
    """Committee ROUND 3, finding 1(ii): the recovery roll-forward is an
    install point, so it must run the same byte-level guard as the forward
    path. A recovery source whose family is LIVE on another mount does NOT
    copy: the journal is cleared (nothing left to roll forward), a CRED-AUDIT
    refusal + [URGENT] line fire, and the lane is left to the relogin/SOS
    machinery — a paused lane beats a guard-free double-book."""
    env = _Env()
    try:
        env.make_slot("beta", live=True)              # beta's family live on a lane
        mover = env.make_slot("alpha", live=True)
        # Journaled source = beta's SNAPSHOT (same family as the live lane) —
        # the world changed between the journal write and recovery.
        cus._write_swap_journal("alpha", "beta", "auto-ladder", slot=mover,
                                install_src=env.accounts_dir / "account-beta" / ".credentials.json",
                                used_independent=False)
        (cus.slot_path(mover) / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "beta@x"}}))
        cus._recover_pending_swap()
        assert not cus._swap_journal_path().exists(), "refused roll-forward must clear the journal"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(mover) / ".credentials.json")) == "rt-alpha", \
            "collision must mean NO copy — the mount keeps its current creds"
        assert cus.load_state()["slots"][mover]["account"] == "alpha", \
            "a refused completion must not half-complete the bookkeeping"
        assert any("op=family-collision-refuse" in e
                   and "refused-recovery-roll-forward" in e for e in env.echoes), env.echoes
        assert any("[URGENT]" in e for e in env.echoes), env.echoes
    finally:
        env.restore()


def test_recovery_roll_forward_blank_source_skips_copy():
    """Committee ROUND 4, finding 1: the recovery roll-forward must apply the
    GH #141 blank/unreadable gate before its copy. A pool-family install_src is
    blank-validated only at the LATE forward gate — which sits PAST the journal
    write and the save-back's real I/O — so a crash in that window journals an
    unvalidated source; pre-fix recovery atomic_copy'd its blank bytes onto the
    live mount (the exact blanked-live-mount incident, arriving via recovery).
    Post-fix: NO copy (the mount keeps its current creds), vanished-source
    semantics otherwise — bookkeeping reconciles (the identity had already
    merged), a SKIPPED note + CRED-AUDIT fire, journal cleared, relogin/SOS
    owns the lane."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("alpha", live=True)
        # Blank-shaped family store (empty accessToken): claimable fail-open
        # under the unverifiable probe, collides with nothing — only the #141
        # gate would catch it, and recovery never reached that gate.
        d = cus.login_family_dir("beta", "family-1")
        d.mkdir(parents=True, exist_ok=True)
        cus.login_family_creds_path("beta", "family-1").write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "", "refreshToken": "rt-beta-fam1",
                               "expiresAt": 2_000_000_000_000}}))
        # Crash simulation: journal written (with the blank, never-validated
        # source) + identity merged; the crash landed before the late gate.
        cus._write_swap_journal("alpha", "beta", "auto-ladder", slot=mover,
                                install_src=cus.login_family_creds_path("beta", "family-1"),
                                used_independent=True, login_family="beta/family-1")
        (cus.slot_path(mover) / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "beta@x"}}))
        cus._recover_pending_swap()
        assert not cus._swap_journal_path().exists()
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(mover) / ".credentials.json")) == "rt-alpha", \
            "a blank journaled source must mean NO copy — the mount keeps its current creds"
        # Vanished-source semantics: state reconciles to the journal's `to`
        # (the identity merge had already happened) with the copy skipped.
        assert cus.load_state()["slots"][mover]["account"] == "beta"
        assert any("op=blank-source-refuse" in e
                   and "refused-recovery-roll-forward" in e for e in env.echoes), env.echoes
        assert any("SKIPPED" in e and "GH #141" in e for e in env.echoes), env.echoes
    finally:
        env.restore()


def test_recovery_legacy_journal_keeps_snapshot_semantics():
    """Committee ROUND 3, finding 1(iii): a LEGACY journal (no install_src
    field — written by a pre-fix cus) keeps the historic GH #76 behavior:
    recovery completes the install from the account snapshot. beta is live
    nowhere else, so the byte guard stays silent and the copy lands."""
    env = _Env()
    try:
        mover = env.make_slot("alpha", live=True)
        cus.write_json(cus._swap_journal_path(), {
            "from": "alpha", "to": "beta", "trigger": "auto-ladder",
            "slot": mover, "ts": cus.now_iso(),
        })
        (cus.slot_path(mover) / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "beta@x"}}))
        cus._recover_pending_swap()
        assert not cus._swap_journal_path().exists()
        assert cus.load_state()["slots"][mover]["account"] == "beta"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(mover) / ".credentials.json")) == "rt-beta"
    finally:
        env.restore()


def test_refusal_unwind_restore_failure_still_clears_journal():
    """Committee ROUND 3, finding 2: the refusal-unwind used to restore the
    pre-merge .claude.json BEFORE clearing the journal — if the restore itself
    raised (ENOSPC/EIO), the journal survived NEXT TO the merged identity and
    the next swap's recovery completed the REFUSED install. Post-fix the
    journal is cleared FIRST: the same double-fault now degrades to
    identity-drift detection (no journal ⇒ recovery is a no-op), never a
    guard-free install."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        env.make_slot("beta", live=True)              # beta held → pool-claim path
        mover = env.make_slot("alpha", live=True)
        # Blank-shaped family: claimable (probe is "unknown"/fail-open),
        # collides with nothing, and trips the late #141 install-point gate —
        # the one refusal that legitimately fires PAST the journal write.
        d = cus.login_family_dir("beta", "family-1")
        d.mkdir(parents=True, exist_ok=True)
        cus.login_family_creds_path("beta", "family-1").write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "", "refreshToken": "rt-beta-fam1",
                               "expiresAt": 2_000_000_000_000}}))
        lane_cj = cus.slot_path(mover) / ".claude.json"
        orig_write = cus.atomic_write_bytes
        writes = {"n": 0}

        def flaky(path, content, mode=0o644):
            if Path(path) == lane_cj:
                writes["n"] += 1
                if writes["n"] >= 2:      # 1st = the identity merge; 2nd = the unwind restore
                    raise OSError("simulated ENOSPC during the unwind restore")
            return orig_write(path, content, mode=mode)

        cus.atomic_write_bytes = flaky
        try:
            raised = None
            try:
                cus.execute_swap("beta", trigger="auto-ladder", slot=mover)
            except (RuntimeError, OSError) as e:
                raised = e
            assert raised is not None, "the late #141 gate (or the injected restore fault) must raise"
        finally:
            cus.atomic_write_bytes = orig_write
        # Double fault: the identity stayed merged (the restore failed)...
        assert cus.read_json(lane_cj)["oauthAccount"]["emailAddress"] == "beta@x"
        # ...but the journal is GONE, so recovery completes nothing.
        assert not cus._swap_journal_path().exists(), "journal must be cleared BEFORE the cj restore"
        cus._recover_pending_swap()
        assert cus.load_state()["slots"][mover]["account"] == "alpha"
        assert cus._credential_refresh_token(
            cus.read_json(cus.slot_path(mover) / ".credentials.json")) == "rt-alpha"
    finally:
        env.restore()


def test_gate_on_shared_mount_hatch_byte_identical_still_refused():
    """Committee ROUND 3, finding 3 (coverage): gate-ON + slot=None +
    allow_shared_family:true. The name-keyed slot=None guard consults the
    hatch even gate-on (the documented rotation-divergence caveat), but a
    byte-IDENTICAL snapshot must still refuse — pinning the docs' precise
    claim that gate-ON's hatch-independent refusals include byte-identical
    collisions. Same-name shape refuses via the #104 collide guard;
    cross-name (gamma = renamed copy of beta) via the name-agnostic guard."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True,
                                               "allow_shared_family": True}})
        (env.accounts_dir / "account-gamma" / ".credentials.json").write_text(
            json.dumps(_creds("rt-beta")))
        env.make_slot("beta", live=True)              # beta's family live on a lane
        raised_same = ""
        try:
            cus.execute_swap("beta", trigger="manual")            # slot=None: shared mount
        except RuntimeError as e:
            raised_same = str(e)
        assert raised_same and "refresh-token family" in raised_same, raised_same
        raised_cross = ""
        try:
            cus.execute_swap("gamma", trigger="manual")           # renamed copy of beta
        except RuntimeError as e:
            raised_cross = str(e)
        assert raised_cross, "gate-on hatch must not open a byte-identical shared-mount install"
        assert cus._refresh_fingerprint("rt-beta") in raised_cross, raised_cross
        assert cus.load_state()["active"] == "alpha"
        assert cus._credential_refresh_token(cus.read_json(cus.CREDS_JSON)) == "rt-bare"
        assert not cus._swap_journal_path().exists()
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
