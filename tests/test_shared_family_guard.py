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
                       ("HOME", "CLAUDE_DIR", "CREDS_JSON", "ACCOUNTS_DIR", "STATE_JSON", "CONFIG_YAML")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
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
        assert f"cus login-mount beta" in raised_msg, raised_msg
        assert "allow_shared_family" in raised_msg, raised_msg
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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
