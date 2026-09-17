"""Tests for Phases 2.2 + 2.3: cus launch preparation and session-side
slot/account detection.

Plan: docs/plans/2026-07-02-per-session-accounts.md.

Run standalone:  python3 tests/test_launch_and_detection.py
Run under pytest: pytest tests/test_launch_and_detection.py
"""

from __future__ import annotations

import json
import os
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
        (self.claude_dir / ".credentials.json").write_text(json.dumps(_creds("rt-gamma")))
        self.claude_json.write_text(json.dumps({**_identity("gamma"), "mcpServers": {"m": {}}}))

        for name in ("alpha", "beta", "gamma"):
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{name}")))
            (d / ".claude.json").write_text(json.dumps(_identity(name)))

        cus.write_json(self.accounts_dir / "state.json", {
            "active": "gamma",
            # alpha lightly used, beta heavier — auto-pick should prefer alpha
            "accounts": {
                "alpha": {"next_swap_at_pct": 50, "current_5h_pct": 10.0, "current_7d_pct": 10.0},
                "beta": {"next_swap_at_pct": 50, "current_5h_pct": 60.0, "current_7d_pct": 40.0},
                "gamma": {"next_swap_at_pct": 50, "current_5h_pct": 5.0, "current_7d_pct": 5.0},
            },
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
        self._saved_env = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        if self._saved_env is not None:
            os.environ["CLAUDE_CONFIG_DIR"] = self._saved_env
        else:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self._tmp.cleanup()


def test_pick_launch_account_spreads_over_occupied():
    env = _Env()
    try:
        config = cus.load_config()
        state = cus.load_state()
        # gamma is global-active (occupied by bare sessions); nothing slotted:
        # gamma would win on usage but is excluded on the spreading pass.
        t = cus.pick_launch_account(state, config)
        assert t is not None and t.name == "alpha"

        # alpha now occupies a slot too → next launch lands on beta.
        state["slots"] = {"slot-1": {"account": "alpha"}}
        t = cus.pick_launch_account(state, config)
        assert t is not None and t.name == "beta"

        # Everything occupied → doubling up allowed (second pass).
        state["slots"]["slot-2"] = {"account": "beta"}
        t = cus.pick_launch_account(state, config)
        assert t is not None, "occupied-everywhere still yields an account"
    finally:
        env.restore()


def test_acquire_slot_prefers_matching_then_free_then_create():
    env = _Env()
    try:
        state = cus.load_state()
        n1, _ = cus.create_slot(state)
        n2, _ = cus.create_slot(state)
        # acquire_slot reloads state from disk under the lock, so assign
        # accounts + clear the just-created reservations ON DISK (the slots are
        # idle in this test — no live launch to protect).
        state = cus.load_state()
        state["slots"][n1].update({"account": "beta"})
        state["slots"][n2].update({"account": "alpha"})
        for e in state["slots"].values():
            e.pop("reserved_until", None)
        cus.save_state(state)

        name, _ = cus.acquire_slot(state, prefer_account="alpha")
        assert name == n2, "free slot already holding the account wins (no swap needed)"

        # Occupied slots are skipped; none free → new slot created. (Also clear
        # the reservation acquire just put on n2 so it's not the reason.)
        st = cus.load_state()
        for e in st["slots"].values():
            e.pop("reserved_until", None)
        cus.save_state(st)
        cus.mount_pids = lambda mount: [1]
        name, d = cus.acquire_slot(cus.load_state(), prefer_account="alpha")
        assert name == "slot-3"
        cus.mount_pids = lambda mount: []
    finally:
        env.restore()


def test_launch_prepare_full_flow():
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        slot_name, slot_dir, account = cus._launch_prepare("alpha", state, config)
        assert account == "alpha"
        assert (slot_dir / "settings.json").is_symlink(), "slot healed/scaffolded"
        cj = json.loads((slot_dir / ".claude.json").read_text())
        assert cj["userID"] == "uid-alpha", "identity installed"
        assert cj["mcpServers"] == {"m": {}}, "canonical non-account keys synced"
        creds = json.loads((slot_dir / ".credentials.json").read_text())
        assert creds["claudeAiOauth"]["refreshToken"] == "rt-alpha"
        st = cus.load_state()
        assert st["slots"][slot_name]["account"] == "alpha"
        assert st["slots"][slot_name].get("last_launch_ts")
        assert st["active"] == "gamma", "global mount untouched"

        # Relaunching the same account reuses the same slot without a swap —
        # but only once the prior launch's reservation has lapsed (a slot
        # claimed <120s ago is deliberately NOT reused, so concurrent launches
        # don't collide). Simulate the reservation expiring (session came and
        # went idle) by clearing it on disk.
        st = cus.load_state()
        for e in st["slots"].values():
            e.pop("reserved_until", None)
        cus.save_state(st)
        slot_name2, _, _ = cus._launch_prepare("alpha", cus.load_state(), config)
        assert slot_name2 == slot_name
    finally:
        env.restore()


def test_launch_reinstalls_when_slot_holds_account_with_unusable_creds():
    # A slot that already holds the requested account but carries logout-shaped
    # creds (no refreshToken) must be REINSTALLED from the good snapshot on
    # launch — not reused as-is (else the session starts logged out). Without
    # the guard, slot.account == account short-circuits execute_swap and the
    # dead creds would persist.
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        slot_name, slot_dir = cus.create_slot(state)
        state = cus.load_state()
        state["slots"][slot_name].update({"account": "alpha"})
        state["slots"][slot_name].pop("reserved_until", None)
        cus.save_state(state)
        # logout-shaped: accessToken present, NO refreshToken.
        (slot_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "at-dead", "expiresAt": 1}}))
        assert not cus.mount_has_usable_credentials(slot_dir)

        got_slot, got_dir, account = cus._launch_prepare("alpha", cus.load_state(), config)
        assert account == "alpha"
        assert got_slot == slot_name, "reused the same slot (matching account)"
        creds = json.loads((got_dir / ".credentials.json").read_text())
        assert creds["claudeAiOauth"]["refreshToken"] == "rt-alpha", "reinstalled from good snapshot"
    finally:
        env.restore()


def test_launch_reinstalls_when_slot_holds_a_superseded_token_generation():
    # An idle slot already holding the requested account, whose creds are
    # PRESENT and well-formed but carry a SUPERSEDED refresh generation (the
    # snapshot has since rotated), must be reinstalled from the snapshot.
    # Refresh tokens are single-use: reusing the lane's spent copy makes Claude
    # Code refresh, get invalid_grant, and blank the mount — the session comes
    # up "not logged in" (incident 2026-08-06, slot-4: lane held 717ea2c5e6 /
    # expired 11:49, snapshot held 2caf3991735c; the mount was blanked at the
    # launch second and the daemon's reactive lane heal only repaired it 66s
    # later). mount_has_usable_credentials only proves a refresh token is
    # PRESENT, so the existing unusable-creds guard cannot catch this.
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        slot_name, slot_dir = cus.create_slot(state)
        state = cus.load_state()
        state["slots"][slot_name].update({"account": "alpha"})
        state["slots"][slot_name].pop("reserved_until", None)
        cus.save_state(state)
        # Spent generation: parses, HAS a refresh token, but the snapshot's is
        # newer — and its access token has already expired.
        (slot_dir / ".credentials.json").write_text(
            json.dumps(_creds("rt-alpha-spent", expires_at=1_000_000_000_000)))
        assert cus.mount_has_usable_credentials(slot_dir), \
            "precondition: the stale copy looks 'usable' — presence, not generation"

        got_slot, got_dir, account = cus._launch_prepare("alpha", cus.load_state(), config)
        assert account == "alpha"
        assert got_slot == slot_name, "reused the same slot (matching account)"
        creds = json.loads((got_dir / ".credentials.json").read_text())
        assert creds["claudeAiOauth"]["refreshToken"] == "rt-alpha", \
            "reinstalled the snapshot's current generation, not the lane's spent one"
    finally:
        env.restore()


def test_launch_keeps_a_lane_token_the_snapshot_has_not_superseded():
    # Mirror guard: when the lane's own copy is the FRESHER one (it refreshed
    # in place and the save-back hasn't run yet), the launch must leave it
    # alone. Clobbering it with an older snapshot would destroy the live
    # generation — the GH #77 failure this must not reintroduce.
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        slot_name, slot_dir = cus.create_slot(state)
        state = cus.load_state()
        state["slots"][slot_name].update({"account": "alpha"})
        state["slots"][slot_name].pop("reserved_until", None)
        cus.save_state(state)
        (slot_dir / ".credentials.json").write_text(
            json.dumps(_creds("rt-alpha-newer", expires_at=2_100_000_000_000)))

        _, got_dir, _ = cus._launch_prepare("alpha", cus.load_state(), config)
        creds = json.loads((got_dir / ".credentials.json").read_text())
        assert creds["claudeAiOauth"]["refreshToken"] == "rt-alpha-newer", \
            "lane's fresher generation survives the launch"
    finally:
        env.restore()


def test_launch_leaves_a_leased_lane_alone_even_when_the_snapshot_is_fresher():
    # #104 discipline: a lane holding a LEASED login family runs a different
    # refresh-token family from the shared snapshot by design, so the snapshot
    # being "fresher" says nothing about it. Reinstalling would cross-family the
    # mount and clobber every other holder of that snapshot on the next refresh.
    # The superseded check must therefore never fire on a leased lane.
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        slot_name, slot_dir = cus.create_slot(state)
        state = cus.load_state()
        state["slots"][slot_name].update(
            {"account": "alpha", "login_family": "alpha/family-1"})
        state["slots"][slot_name].pop("reserved_until", None)
        cus.save_state(state)
        (slot_dir / ".credentials.json").write_text(
            json.dumps(_creds("rt-alpha-family-1", expires_at=1_000_000_000_000)))
        assert cus._mount_creds_superseded(slot_dir, "alpha"), \
            "precondition: the shared snapshot IS strictly fresher"

        # Healthy independent lineage: the launch liveness gate probes an expired
        # mount and must leave an ALIVE leased/legacy lane alone.
        _saved_grant = cus._oauth_refresh_grant
        cus._STORE_DEAD_PROBE.clear()
        cus._oauth_refresh_grant = lambda rt: ("alive", {})
        try:
            _, got_dir, _ = cus._launch_prepare("alpha", cus.load_state(), config)
        finally:
            cus._oauth_refresh_grant = _saved_grant
        creds = json.loads((got_dir / ".credentials.json").read_text())
        assert creds["claudeAiOauth"]["refreshToken"] == "rt-alpha-family-1", \
            "leased family survives — never cross-familied from the shared snapshot"
    finally:
        env.restore()


def test_launch_leaves_a_legacy_independent_login_lane_alone():
    # The OTHER independent-login lineage, and the one state cannot show you: a
    # LEGACY per-(slot, account) login leaves no `login_family` entry, so
    # `slot_leased_family` returns None for it. It is still not comparable
    # against the shared snapshot — `swap_install_source` installs it from its
    # own store — so the superseded check must skip it exactly as it skips a
    # pooled lease, or the reinstall re-families the mount (#104).
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        config["independent_logins"] = {"use_independent_logins": True}
        slot_name, slot_dir = cus.create_slot(state)
        state = cus.load_state()
        state["slots"][slot_name].update({"account": "alpha"})
        state["slots"][slot_name].pop("reserved_until", None)
        cus.save_state(state)
        # Legacy store for THIS (account, slot) — note: no login_family in state.
        # The store deliberately holds an OLDER in-lineage generation than the
        # mount (the lane refreshed in place; the save-back hasn't run), so a
        # wrongful reinstall is VISIBLE: whichever source it picks — the legacy
        # store or, if the lease verdict isn't "ok", the shared snapshot — the
        # mount's own newer token is destroyed either way.
        store = cus.login_store_creds_path("alpha", slot_name)
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(_creds("rt-alpha-legacy-v1", expires_at=1_200_000_000_000)))
        (slot_dir / ".credentials.json").write_text(
            json.dumps(_creds("rt-alpha-legacy-v2", expires_at=1_500_000_000_000)))
        assert cus.slot_leased_family(cus.load_state(), slot_name) is None, \
            "precondition: a legacy login is invisible to slot_leased_family"
        assert cus.has_independent_login("alpha", slot_name)
        assert cus._mount_creds_superseded(slot_dir, "alpha"), \
            "precondition: the shared snapshot IS a different, strictly fresher generation"

        # Healthy independent lineage: the launch liveness gate probes an expired
        # mount and must leave an ALIVE leased/legacy lane alone.
        _saved_grant = cus._oauth_refresh_grant
        cus._STORE_DEAD_PROBE.clear()
        cus._oauth_refresh_grant = lambda rt: ("alive", {})
        try:
            _, got_dir, _ = cus._launch_prepare("alpha", cus.load_state(), config)
        finally:
            cus._oauth_refresh_grant = _saved_grant
        creds = json.loads((got_dir / ".credentials.json").read_text())
        assert creds["claudeAiOauth"]["refreshToken"] == "rt-alpha-legacy-v2", \
            "legacy lineage survives — never re-familied from the shared snapshot"
    finally:
        env.restore()


def test_launch_restores_the_lane_when_the_reinstall_is_refused():
    # The unfit path blanks the lane's account BEFORE execute_swap runs, so a
    # refusal (dead snapshot, pool exhausted, #15 shared-family fail-closed)
    # must not leave state saying the lane holds nothing while its mount still
    # carries the old credentials — an accountless lane is free for the
    # allocator to hand to someone else. The launch still fails; it just fails
    # without stranding the lane. (This is the force-launch behavior change:
    # a superseded lane now reaches execute_swap, which can legitimately refuse,
    # where before it silently reused the dead grant.)
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        slot_name, slot_dir = cus.create_slot(state)
        state = cus.load_state()
        state["slots"][slot_name].update({"account": "alpha"})
        state["slots"][slot_name].pop("reserved_until", None)
        cus.save_state(state)
        (slot_dir / ".credentials.json").write_text(
            json.dumps(_creds("rt-alpha-spent", expires_at=1_000_000_000_000)))

        import click
        saved = cus.execute_swap
        cus.execute_swap = lambda *a, **k: (_ for _ in ()).throw(
            click.ClickException("pool exhausted for 'alpha'"))
        try:
            cus._launch_prepare("alpha", cus.load_state(), config)
            raise AssertionError("expected the refusal to propagate")
        except click.ClickException as exc:
            assert "pool exhausted" in str(exc)
        finally:
            cus.execute_swap = saved

        assert cus.load_state()["slots"][slot_name]["account"] == "alpha", \
            "lane's recorded account restored after the refused reinstall"
    finally:
        env.restore()


def test_launch_reinstall_does_not_save_the_spent_creds_back_over_the_snapshot():
    # The blank before execute_swap is load-bearing twice, and this pins both.
    # _execute_swap_locked reads the outgoing account from
    # state.slots[slot].account: were the lane still recorded as holding
    # 'alpha', (1) target_name == current would return early and install
    # NOTHING, and (2) it would first save the lane's SPENT credentials back
    # over the account snapshot — destroying the fresher generation the whole
    # reinstall exists to install. A future refactor that reinstalls without
    # blanking first reintroduces both, silently.
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        slot_name, slot_dir = cus.create_slot(state)
        state = cus.load_state()
        state["slots"][slot_name].update({"account": "alpha"})
        state["slots"][slot_name].pop("reserved_until", None)
        cus.save_state(state)
        (slot_dir / ".credentials.json").write_text(
            json.dumps(_creds("rt-alpha-spent", expires_at=1_000_000_000_000)))
        snap = env.accounts_dir / "account-alpha" / ".credentials.json"

        cus._launch_prepare("alpha", cus.load_state(), config)

        assert json.loads(snap.read_text())["claudeAiOauth"]["refreshToken"] == "rt-alpha", \
            "snapshot intact — the lane's spent generation was NOT saved back over it"
        assert json.loads((slot_dir / ".credentials.json").read_text()
                          )["claudeAiOauth"]["refreshToken"] == "rt-alpha", \
            "and the lane actually received the snapshot (the swap did not no-op)"
    finally:
        env.restore()


def test_launch_clears_the_reservation_it_set_when_the_reinstall_is_refused():
    # The reservation earns its keep during the blank→swap window, but once the
    # reinstall has refused there is no launch in flight: leaving it set holds
    # the lane non-allocatable AND non-reapable for SLOT_RESERVATION_SECONDS and
    # makes the daemon gc report `refused_reserved` meanwhile.
    #
    # Exercised through --lane, which is the case that matters: an explicit-lane
    # launch never passes through acquire_slot, so it carries no reservation of
    # its own and the only one present is the one the blank added. (On the
    # auto-pick path acquire_slot has already reserved the lane for this launch,
    # and the undo correctly restores THAT — a failed launch leaving its own
    # acquire-time reservation behind is pre-existing behavior, unchanged here.
    # What must not happen either way is this code re-extending a reservation on
    # a lane nobody is launching on, which is what a repeatedly-failing
    # `cus launch --lane X` would otherwise do.)
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        slot_name, slot_dir = cus.create_slot(state)
        state = cus.load_state()
        state["slots"][slot_name].update({"account": "alpha"})
        state["slots"][slot_name].pop("reserved_until", None)
        cus.save_state(state)
        (slot_dir / ".credentials.json").write_text(
            json.dumps(_creds("rt-alpha-spent", expires_at=1_000_000_000_000)))
        assert "reserved_until" not in cus.load_state()["slots"][slot_name]

        import click
        saved = cus.execute_swap
        cus.execute_swap = lambda *a, **k: (_ for _ in ()).throw(
            click.ClickException("pool exhausted for 'alpha'"))
        try:
            cus._launch_prepare("alpha", cus.load_state(), config, lane=slot_name)
        except click.ClickException:
            pass
        finally:
            cus.execute_swap = saved

        entry = cus.load_state()["slots"][slot_name]
        assert entry["account"] == "alpha", "account restored"
        assert "reserved_until" not in entry, \
            "reservation restored to its prior (absent) state — no lingering gc noise"
    finally:
        env.restore()


def test_launch_reinstalls_superseded_idle_lane_in_place():
    """An idle lane holding the account on a SUPERSEDED token generation (the
    canonical has rotated past it) is reinstalled in place at launch via a
    forced same-account swap — the slot record never blanks, so no other launch
    can be handed the slot mid-heal (2026-08-06 slot-4 incident)."""
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        slot_name, slot_dir = cus.create_slot(state)
        state = cus.load_state()
        state["slots"][slot_name].update({"account": "alpha"})
        state["slots"][slot_name].pop("reserved_until", None)
        cus.save_state(state)
        (slot_dir / ".credentials.json").write_text(
            json.dumps(_creds("rt-alpha-spent", expires_at=1_000_000_000_000)))
        seen = {}
        saved = cus.execute_swap
        def _spy(*a, **k):
            st = cus.load_state()
            seen["entry"] = dict(st["slots"][slot_name])
            seen["kwargs"] = dict(k)
            return saved(*a, **k)
        cus.execute_swap = _spy
        try:
            cus._launch_prepare("alpha", cus.load_state(), config)
        finally:
            cus.execute_swap = saved
        assert seen["kwargs"].get("force_reinstall") is True, seen
        assert seen["kwargs"].get("slot") == slot_name
        assert seen["entry"]["account"] == "alpha", "in-place reinstall never blanks the slot record"
        assert cus._slot_busy(slot_name, seen["entry"]), \
            "the lane stays held throughout, so the allocator cannot hand it to another launch"
        mount = cus.read_json(slot_dir / ".credentials.json")
        assert cus._credential_refresh_token(mount) != "rt-alpha-spent", "spent generation replaced"
    finally:
        env.restore()


def test_launch_treats_an_unparseable_login_family_as_leased():
    # slot_leased_family returns None for any login_family without a "/" (a bare
    # "family-1" from hand-edited or legacy-shaped state). Reading that as "no
    # lease" would classify a leased lane as PLAIN and let the shared snapshot
    # cross-family its mount. Unknown lineage must not be reinstalled.
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        slot_name, slot_dir = cus.create_slot(state)
        state = cus.load_state()
        state["slots"][slot_name].update({"account": "alpha", "login_family": "family-1"})
        state["slots"][slot_name].pop("reserved_until", None)
        cus.save_state(state)
        (slot_dir / ".credentials.json").write_text(
            json.dumps(_creds("rt-alpha-family-1", expires_at=1_000_000_000_000)))
        assert cus.slot_leased_family(cus.load_state(), slot_name) is None, \
            "precondition: the malformed value does not parse into a lease"

        # Healthy independent lineage: the launch liveness gate probes an expired
        # mount and must leave an ALIVE leased/legacy lane alone.
        _saved_grant = cus._oauth_refresh_grant
        cus._STORE_DEAD_PROBE.clear()
        cus._oauth_refresh_grant = lambda rt: ("alive", {})
        try:
            _, got_dir, _ = cus._launch_prepare("alpha", cus.load_state(), config)
        finally:
            cus._oauth_refresh_grant = _saved_grant
        creds = json.loads((got_dir / ".credentials.json").read_text())
        assert creds["claudeAiOauth"]["refreshToken"] == "rt-alpha-family-1", \
            "unparseable lease is treated as leased, not plain — mount untouched"
    finally:
        env.restore()


def test_unjudgeable_mount_expiry_still_requires_a_live_snapshot():
    # A bool `expiresAt` on the mount makes the ORDERING comparison meaningless
    # (True is an int subclass, so it reads as epoch-ms 1) — but it must not
    # excuse the snapshot from the "not itself already expired" bar. Short-
    # circuiting to True here would install a dead generation over the lane and
    # the session would come up logged out with no relogin prompt, which is the
    # case that has to keep reaching the SOS. Reachable in practice: a DISABLED
    # account's snapshot goes stale because the daemon stops refreshing it.
    env = _Env()
    try:
        _, slot_dir = cus.create_slot(cus.load_state())
        mount = slot_dir / ".credentials.json"
        snap = env.accounts_dir / "account-alpha" / ".credentials.json"
        now_ms = 1_500_000_000_000
        mount.write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "at-x", "refreshToken": "rt-bool",
                               "expiresAt": True}}))

        # Snapshot already expired → NOT an upgrade, however unjudgeable the
        # mount is. Installing it cannot log the session back in.
        snap.write_text(json.dumps(_creds("rt-alpha", expires_at=1_400_000_000_000)))
        assert not cus._mount_creds_superseded(slot_dir, "alpha", now_ms=now_ms), \
            "dead snapshot is never an upgrade, even over an unjudgeable mount"

        # Snapshot still live → the unjudgeable mount is repaired from it.
        snap.write_text(json.dumps(_creds("rt-alpha", expires_at=2_000_000_000_000)))
        assert cus._mount_creds_superseded(slot_dir, "alpha", now_ms=now_ms), \
            "live snapshot does supersede an unjudgeable mount"
    finally:
        env.restore()


def test_mount_creds_superseded_is_conservative():
    # The predicate's three refusals, which keep it from ever making things
    # worse. Mirrors _repair_stale_lane_mount's bar exactly.
    env = _Env()
    try:
        _, slot_dir = cus.create_slot(cus.load_state())
        mount = slot_dir / ".credentials.json"
        snap = env.accounts_dir / "account-alpha" / ".credentials.json"
        now_ms = 1_500_000_000_000

        mount.write_text(json.dumps(_creds("rt-old", expires_at=1_000_000_000_000)))
        assert cus._mount_creds_superseded(slot_dir, "alpha", now_ms=now_ms), \
            "strictly fresher + still valid + refresh-capable → supersede"

        # Equal expiry is the SAME generation — copying it changes nothing.
        mount.write_text(json.dumps(_creds("rt-same", expires_at=2_000_000_000_000)))
        assert not cus._mount_creds_superseded(slot_dir, "alpha", now_ms=now_ms)

        # SAME refresh token, older access-token expiry: a pure access-token
        # re-mint, not a rotation (_refresh_account_token keeps the incumbent
        # when the endpoint returns no new one), so the lane's grant is intact
        # and there is nothing to supersede. Expiry ordering alone would fire
        # here and churn a blank + execute_swap on every idle lane after every
        # daemon refresh.
        mount.write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "at-older", "refreshToken": "rt-alpha",
                               "expiresAt": 1_000_000_000_000}}))
        assert not cus._mount_creds_superseded(slot_dir, "alpha", now_ms=now_ms)

        # A snapshot that is itself already expired cannot log anyone back in;
        # that lane is a genuine relogin case (must reach the SOS, not a copy).
        snap.write_text(json.dumps(_creds("rt-alpha", expires_at=1_400_000_000_000)))
        mount.write_text(json.dumps(_creds("rt-old", expires_at=1_000_000_000_000)))
        assert not cus._mount_creds_superseded(slot_dir, "alpha", now_ms=now_ms)

        # Refresh-token-LESS snapshot: a longer runway with no refresh is not
        # an upgrade over a shorter one that can actually rotate (#13 round-3).
        snap.write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "at-x", "expiresAt": 2_000_000_000_000}}))
        assert not cus._mount_creds_superseded(slot_dir, "alpha", now_ms=now_ms)
    finally:
        env.restore()


def test_launch_prepare_rejects_unknown_account():
    env = _Env()
    try:
        import click
        state = cus.load_state()
        config = cus.load_config()
        try:
            cus._launch_prepare("nope", state, config)
            raise AssertionError("expected ClickException")
        except click.ClickException:
            pass
    finally:
        env.restore()


def _break_slot_projects(slot_dir, shared_projects, divergent: bool):
    """Turn a slot's projects/ into the GH #192 incident shape: a REAL dir
    whose per-project child collides with the shared tree. divergent=True
    plants a same-path file with DIFFERENT bytes (unhealable — operator
    judgment); divergent=False plants only a unique stray transcript
    (healable by the recursive merge)."""
    link = slot_dir / "projects"
    if link.is_symlink():
        link.unlink()
    proj = link / "-home-user-repo"
    proj.mkdir(parents=True)
    (shared_projects / "-home-user-repo").mkdir(exist_ok=True)
    (proj / "sess-stray.jsonl").write_text("stray")
    if divergent:
        (proj / "sess-clash.jsonl").write_text("slot-version")
        (shared_projects / "-home-user-repo" / "sess-clash.jsonl").write_text("shared-version")


def test_launch_prepare_repairs_broken_projects_slot():
    """GH #192 bug 1+2: a slot whose projects/ is a real dir (colliding with
    the shared tree by project-dir NAME) must be actually repaired by the
    launch pre-flight — transcript reunited with the shared tree, dir
    relinked — instead of exec'ing claude against an isolated projects/
    ("No conversation found" on --resume)."""
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        name, d = cus.create_slot(state)
        st = cus.load_state()
        st["slots"][name].pop("reserved_until", None)  # slot is idle, make it acquirable
        cus.save_state(st)
        _break_slot_projects(d, env.claude_dir / "projects", divergent=False)

        slot_name, slot_dir, _ = cus._launch_prepare("alpha", cus.load_state(), config)
        assert slot_name == name, "healable slot is repaired and USED, not skipped"
        assert (slot_dir / "projects").is_symlink()
        assert (slot_dir / "projects").resolve() == (env.claude_dir / "projects").resolve()
        assert (env.claude_dir / "projects" / "-home-user-repo" / "sess-stray.jsonl").read_text() == "stray", \
            "stray transcript merged into the shared tree"
    finally:
        env.restore()


def test_launch_prepare_skips_unhealable_projects_slot():
    """GH #192: when the heal genuinely cannot complete (divergent same-path
    file content), auto-selection must EXCLUDE the broken slot and land on
    one whose projects/ resolves to the shared tree — never exec onto the
    isolated dir. The broken slot's divergent data is left intact."""
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        name, d = cus.create_slot(state)
        st = cus.load_state()
        st["slots"][name].pop("reserved_until", None)
        cus.save_state(st)
        _break_slot_projects(d, env.claude_dir / "projects", divergent=True)

        slot_name, slot_dir, _ = cus._launch_prepare("alpha", cus.load_state(), config)
        assert slot_name != name, "unhealable slot must be skipped in auto selection"
        assert (slot_dir / "projects").resolve() == (env.claude_dir / "projects").resolve(), \
            "chosen slot's projects/ verified against the shared tree pre-exec"
        assert (d / "projects" / "-home-user-repo" / "sess-clash.jsonl").read_text() == "slot-version", \
            "divergent content preserved on the abandoned slot (never destroyed)"
    finally:
        env.restore()


def test_launch_prepare_lane_refuses_unhealable_projects():
    """GH #192: an EXPLICIT --lane onto an unhealable slot fails loudly (the
    operator pinned it on purpose — landing elsewhere silently would be
    worse), instead of exec'ing a session forked off the shared tree."""
    env = _Env()
    try:
        import click
        state = cus.load_state()
        config = cus.load_config()
        name, d = cus.create_slot(state)
        st = cus.load_state()
        st["slots"][name].pop("reserved_until", None)
        cus.save_state(st)
        _break_slot_projects(d, env.claude_dir / "projects", divergent=True)

        try:
            cus._launch_prepare("alpha", cus.load_state(), config, lane=name)
            raise AssertionError("expected ClickException for unhealable --lane projects/")
        except click.ClickException as e:
            assert "GH #192" in str(e.message)
    finally:
        env.restore()


def test_mount_account_from_env():
    env = _Env()
    try:
        state = cus.load_state()
        state["slots"] = {"slot-1": {"account": "alpha"}}

        # Unset → bare launch.
        assert cus.mount_account_from_env(state) == (None, None)

        # Slot dir → slot's current occupant (state-resolved, per render).
        os.environ["CLAUDE_CONFIG_DIR"] = str(env.accounts_dir / "slot-1")
        assert cus.mount_account_from_env(state) == ("slot-1", "alpha")
        state["slots"]["slot-1"]["account"] = "beta"  # swap moved the slot
        assert cus.mount_account_from_env(state) == ("slot-1", "beta")

        # Account dir → the account itself (relogin-style launch).
        os.environ["CLAUDE_CONFIG_DIR"] = str(env.accounts_dir / "account-merkos")
        assert cus.mount_account_from_env(state) == ("account-merkos", "merkos")

        # Foreign path → bare.
        os.environ["CLAUDE_CONFIG_DIR"] = "/somewhere/else"
        assert cus.mount_account_from_env(state) == (None, None)
    finally:
        env.restore()


def test_statusline_shows_slot_hardpin_badge():
    """A slot session's statusline shows the 🔒<slot> hard-pin badge and the
    slot's account; a bare session (no CLAUDE_CONFIG_DIR) shows neither."""
    from click.testing import CliRunner
    env = _Env()
    try:
        state = cus.load_state()
        name, slot_dir = cus.create_slot(state)
        cus.execute_swap("alpha", trigger="launch", slot=name)
        runner = CliRunner()

        # Slot session: badge + slot's account (alpha), color off for asserts.
        r = runner.invoke(cus.cli, ["statusline", "--compact"],
                          env={"CLAUDE_CONFIG_DIR": str(slot_dir), "NO_COLOR": "1"})
        assert r.exit_code == 0, r.output
        assert f"🔒{name}" in r.output, r.output
        assert "alpha" in r.output

        # Bare session: no badge, shows global active (gamma).
        r2 = runner.invoke(cus.cli, ["statusline", "--compact"],
                           env={"CLAUDE_CONFIG_DIR": None, "NO_COLOR": "1"})
        assert r2.exit_code == 0, r2.output
        assert "🔒" not in r2.output, r2.output
    finally:
        env.restore()


def test_pick_launch_account_lane_share_fallback():
    """Saturated regime (every healthy account on a live mount): lane_sharing
    off preserves the #104 refusal (None); lane_sharing on returns the
    lowest-usage live account so _launch_prepare can JOIN its lane
    (2026-07-03 — `cus launch auto` used to be dead whenever slots saturated
    the pool, even with a near-idle account joinable)."""
    env = _Env()
    try:
        state = cus.load_state()
        state["slots"] = {"slot-1": {"account": "alpha"}, "slot-2": {"account": "beta"}}
        for s in ("slot-1", "slot-2"):
            cus.slot_path(s).mkdir(parents=True, exist_ok=True)
        live = {str(cus.slot_path("slot-1")), str(cus.slot_path("slot-2")), str(cus.CLAUDE_DIR)}
        cus.mount_pids = lambda mount: [1] if str(mount) in live else []
        cus._OCCUPIED_SLOTS_CACHE.clear()

        config = cus.load_config()
        assert cus.pick_launch_account(state, config) is None, \
            "lane_sharing off: all-live pool still refuses (#104)"

        cus._OCCUPIED_SLOTS_CACHE.clear()
        config = cus.deep_merge(config, {"per_session": {"lane_sharing": True}})
        t = cus.pick_launch_account(state, config)
        # gamma (5%, the shared-mount active) is the lowest-usage live account.
        assert t is not None and t.name == "gamma", t
        assert "lane-share fallback" in t.reason
    finally:
        env.restore()


def test_launch_prepare_joins_shared_mount():
    """Launching the shared-mount active with live bare sessions: lane_sharing
    on JOINS the global pair (bare session — 'merkos should be a legal
    target'); off keeps the #104 refusal."""
    import click
    env = _Env()
    try:
        live = {str(cus.CLAUDE_DIR)}
        cus.mount_pids = lambda mount: [1] if str(mount) in live else []
        cus._OCCUPIED_SLOTS_CACHE.clear()
        state = cus.load_state()

        config = cus.load_config()
        try:
            cus._launch_prepare("gamma", state, config)
            raise AssertionError("expected ClickException with lane_sharing off")
        except click.ClickException:
            pass

        config = cus.deep_merge(config, {"per_session": {"lane_sharing": True}})
        slot_name, slot_dir, account = cus._launch_prepare("gamma", cus.load_state(), config)
        assert slot_name == "shared"
        assert slot_dir == cus.CLAUDE_DIR
        assert account == "gamma"
    finally:
        env.restore()


class _PinnedLaneEnv:
    """_Env plus one LIVE slot: slot-1 holds alpha with a live mount.
    The recurring sentinel-recycle failure shape (2026-07-15..24): a session
    pinned to a lane relaunches onto it while the lane is still/again live."""

    def __init__(self, locked: bool = False) -> None:
        self.env = _Env()
        state = cus.load_state()
        cus.slot_path("slot-1").mkdir(parents=True, exist_ok=True)
        state["slots"] = {"slot-1": {"account": "alpha"}}
        cus.save_state(state)
        live = {str(cus.slot_path("slot-1"))}
        cus.mount_pids = lambda mount: [1] if str(mount) in live else []
        cus._OCCUPIED_SLOTS_CACHE.clear()
        self.config = cus.deep_merge(cus.load_config(),
                                     {"per_session": {"lane_sharing": True}})
        if locked:
            self.config = cus.deep_merge(self.config,
                                         {"session_locks": {"locked_slots": ["slot-1"]}})
        # The auto path's fresh-reading verify loop must not really poll.
        self._saved_poll = cus._force_poll_launch_candidate
        cus._force_poll_launch_candidate = lambda *a, **k: False

    def restore(self) -> None:
        cus._force_poll_launch_candidate = self._saved_poll
        self.env.restore()


def test_launch_prepare_auto_joins_live_pinned_lane():
    """`cus launch auto --lane slot-1` with slot-1 LIVE on alpha must JOIN
    alpha's lane (the only launch that can succeed there) instead of
    auto-picking a different account and dying on the
    "--lane slot-1 is live on 'alpha', not 'X'" refusal."""
    p = _PinnedLaneEnv()
    try:
        slot_name, slot_dir, account = cus._launch_prepare(
            "auto", cus.load_state(), p.config, lane="slot-1")
        assert (slot_name, account) == ("slot-1", "alpha")
        assert slot_dir == cus.slot_path("slot-1")
        assert cus.load_state()["slots"]["slot-1"].get("last_launch_ts")
    finally:
        p.restore()


def test_launch_prepare_explicit_occupant_joins_live_pinned_lane():
    """`cus launch alpha --lane slot-1` with slot-1 LIVE on alpha must JOIN —
    today it dies at the GH #104 duplicate-mount guard even though joining the
    lane creates no second mount."""
    p = _PinnedLaneEnv()
    try:
        slot_name, slot_dir, account = cus._launch_prepare(
            "alpha", cus.load_state(), p.config, lane="slot-1")
        assert (slot_name, account) == ("slot-1", "alpha")
    finally:
        p.restore()


def test_launch_prepare_explicit_other_account_still_refuses_live_lane():
    """`cus launch beta --lane slot-1` with slot-1 LIVE on alpha stays a
    refusal (a genuinely conflicting explicit request)."""
    import click
    p = _PinnedLaneEnv()
    try:
        try:
            cus._launch_prepare("beta", cus.load_state(), p.config, lane="slot-1")
            raise AssertionError("expected ClickException")
        except click.ClickException as e:
            assert "is live on" in str(e.message)
    finally:
        p.restore()


def test_launch_prepare_pinned_join_respects_locked_slot():
    """A LIVE locked lane must not gain sessions via the pinned-lane join:
    the lock refusal wins (mirroring the explicit-lane lock guard)."""
    import click
    p = _PinnedLaneEnv(locked=True)
    try:
        try:
            cus._launch_prepare("auto", cus.load_state(), p.config, lane="slot-1")
            raise AssertionError("expected ClickException")
        except click.ClickException as e:
            assert "locked slot" in str(e.message)
    finally:
        p.restore()


def test_launch_prepare_pinned_join_returns_fresh_occupant():
    """The join reloads state before persisting; if a daemon in-place move
    changed the lane's account between the caller's snapshot and that reload,
    the returned account must be the FRESH occupant — the mount's real
    content — not the snapshot's (Copilot review, PR #12)."""
    p = _PinnedLaneEnv()
    try:
        stale = cus.load_state()  # snapshot: slot-1 -> alpha
        st = cus.load_state()
        st["slots"]["slot-1"]["account"] = "beta"  # daemon moved the lane
        cus.save_state(st)
        slot_name, _, account = cus._launch_prepare(
            "auto", stale, p.config, lane="slot-1")
        assert (slot_name, account) == ("slot-1", "beta")
    finally:
        p.restore()


def test_launch_prepare_pinned_lane_without_lane_sharing_keeps_refusal():
    """lane_sharing off: the pinned-lane join stays disabled and the existing
    "is live on ... Pick a free lane" refusal is preserved."""
    import click
    p = _PinnedLaneEnv()
    try:
        config = cus.deep_merge(cus.load_config(),
                                {"per_session": {"lane_sharing": False}})
        try:
            cus._launch_prepare("auto", cus.load_state(), config, lane="slot-1")
            raise AssertionError("expected ClickException")
        except click.ClickException as e:
            assert "Pick a free lane" in str(e.message) or "live mount" in str(e.message)
    finally:
        p.restore()


def test_launch_swap_does_not_arm_ladder_hysteresis():
    """trigger='launch' bumps last_swap_ts (display) but NOT last_auto_swap_ts
    (the ladder cooldown clock) — a launch isn't ladder churn (2026-07-03:
    launches kept re-arming a 50-min cooldown, parking hot slots). A daemon
    trigger arms both."""
    env = _Env()
    try:
        state = cus.load_state()
        name, _slot_dir = cus.create_slot(state)
        cus.execute_swap("alpha", trigger="launch", slot=name)
        st = cus.load_state()
        assert st["accounts"]["alpha"].get("last_swap_ts")
        assert "last_auto_swap_ts" not in st["accounts"]["alpha"]

        cus.execute_swap("beta", trigger="auto-ladder", slot=name)
        st = cus.load_state()
        assert st["accounts"]["beta"].get("last_auto_swap_ts")
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# Issue #219: a `cus lock <slot>` slot must be EXCLUSIVE under lane sharing —
# never joined as a co-tenant, never picked as an auto lane-share target,
# never pinned onto by an explicit --lane (without --force). The lock used to
# only stop the daemon MOVING/GC-ing the slot; it did not reserve it against
# co-tenancy, so a work session could share the watchdog's locked slot-2.
# ---------------------------------------------------------------------------
def _seed_live_lane(name: str, account: str) -> Path:
    """Scaffold a slot dir as a HEALTHY, joinable lane holding `account`'s creds:
    projects/ symlinked to the shared tree (so _projects_resolves_to_shared
    passes) and a valid OAuth payload installed (so the launch-gate shape check
    accepts the join). Returns the slot dir. State bookkeeping is the caller's."""
    d = cus.slot_path(name)
    cus.scaffold_mount_dir(d)  # dir + projects→shared symlink + settings links
    cus.mount_creds_path(d).write_text(json.dumps(_creds(f"rt-{account}")))
    return d


def test_launch_prepare_join_skips_locked_lane():
    """Issue #219 (Part 1): an auto lane-share JOIN must skip a LOCKED occupied
    lane. Given account alpha live on [locked slot-2, unlocked slot-5], the join
    picks slot-5, never the locked slot-2. And when alpha's ONLY live lane is
    the locked one, the join selects nothing and falls through to the #104
    duplicate-mount refusal (no silent co-tenancy onto a locked lane)."""
    import click
    env = _Env()
    try:
        # alpha occupies a locked lane (slot-2) and an unlocked lane (slot-5).
        _seed_live_lane("slot-2", "alpha")
        _seed_live_lane("slot-5", "alpha")
        state = cus.load_state()
        state["slots"] = {"slot-2": {"account": "alpha"}, "slot-5": {"account": "alpha"}}
        cus.save_state(state)
        live = {str(cus.slot_path("slot-2")), str(cus.slot_path("slot-5"))}
        cus.mount_pids = lambda mount: [1] if str(mount) in live else []
        cus._OCCUPIED_SLOTS_CACHE.clear()

        config = cus.deep_merge(cus.load_config(), {
            "per_session": {"lane_sharing": True},
            "session_locks": {"locked_slots": ["slot-2"]},
        })
        slot_name, slot_dir, account = cus._launch_prepare("alpha", cus.load_state(), config)
        assert slot_name == "slot-5", f"join must pick the unlocked lane, got {slot_name}"
        assert account == "alpha"
        assert slot_dir == cus.slot_path("slot-5")

        # Lock BOTH of alpha's lanes → the join has no eligible lane and falls
        # through to the #104 guard (alpha is live on a mount, not the shared
        # active, no independent login provisioned) → refusal, not co-tenancy.
        cus._OCCUPIED_SLOTS_CACHE.clear()
        config2 = cus.deep_merge(cus.load_config(), {
            "per_session": {"lane_sharing": True},
            "session_locks": {"locked_slots": ["slot-2", "slot-5"]},
        })
        try:
            cus._launch_prepare("alpha", cus.load_state(), config2)
            raise AssertionError("expected #104 refusal when alpha's only lanes are locked")
        except click.ClickException as e:
            assert "GH #104" in str(e.message), e.message
    finally:
        env.restore()


def test_pick_launch_account_lane_share_skips_locked_only_account():
    """Issue #219 (Part 2): pick_launch_account's lane-share fallback must not
    pick an account whose ONLY live-occupied lane is locked — such an account
    has no lane a launch could actually join, so choosing it would make
    `cus launch auto` pick an account it then refuses (#104). It prefers an
    account with a non-locked lane; when every live lane is locked it yields no
    lane-share target (None)."""
    env = _Env()
    try:
        state = cus.load_state()
        # Only alpha + beta in play: drop gamma from `accounts` (state["active"]
        # stays "gamma", but the shared mount is kept NOT live below, so the
        # shared-active account is never a candidate) — this isolates the
        # lane-share fallback under test so the earlier picker tiers can't hand
        # back an idle account.
        state["accounts"].pop("gamma", None)
        state["slots"] = {"slot-1": {"account": "alpha"}, "slot-2": {"account": "beta"}}
        for s in ("slot-1", "slot-2"):
            cus.slot_path(s).mkdir(parents=True, exist_ok=True)
        live = {str(cus.slot_path("slot-1")), str(cus.slot_path("slot-2"))}  # shared NOT live
        cus.mount_pids = lambda mount: [1] if str(mount) in live else []
        cus._OCCUPIED_SLOTS_CACHE.clear()

        # alpha's only lane (slot-1) is locked; beta's lane (slot-2) is not.
        config = cus.deep_merge(cus.load_config(), {
            "per_session": {"lane_sharing": True},
            "session_locks": {"locked_slots": ["slot-1"]},
        })
        t = cus.pick_launch_account(state, config)
        assert t is not None and t.name == "beta", t
        assert "lane-share fallback" in t.reason

        # Lock BOTH lanes → no joinable account at all → no lane-share target.
        cus._OCCUPIED_SLOTS_CACHE.clear()
        config = cus.deep_merge(cus.load_config(), {
            "per_session": {"lane_sharing": True},
            "session_locks": {"locked_slots": ["slot-1", "slot-2"]},
        })
        assert cus.pick_launch_account(state, config) is None, \
            "every live lane locked ⇒ no lane-share target"
    finally:
        env.restore()


def test_launch_prepare_explicit_lane_refuses_locked():
    """Issue #219 (Part 3, pre-existing since 2026-07-08 commit 940cc65): an
    explicit `--lane <locked-slot>` refuses without --force (mirroring
    `cus slot move`'s lock guard) and proceeds with --force. This test locks in
    that behavior alongside the two new #219 fixes."""
    import click
    env = _Env()
    try:
        state = cus.load_state()
        name, _d = cus.create_slot(state)  # a free slot to pin onto
        st = cus.load_state()
        st["slots"][name].pop("reserved_until", None)  # make it acquirable
        cus.save_state(st)

        config = cus.deep_merge(cus.load_config(), {
            "session_locks": {"locked_slots": [name]},
        })
        try:
            cus._launch_prepare("alpha", cus.load_state(), config, lane=name)
            raise AssertionError("expected ClickException for --lane onto a locked slot")
        except click.ClickException as e:
            assert name in str(e.message) and "locked" in str(e.message), e.message

        # --force overrides, exactly like `cus slot move --force`.
        slot_name, _slot_dir, account = cus._launch_prepare(
            "alpha", cus.load_state(), config, lane=name, force=True)
        assert slot_name == name and account == "alpha"
    finally:
        env.restore()


def test_launch_prepare_force_joins_locked_only_lane():
    """Issue #219 / PR #220 dual review (C5a — GUARDS C1). `cus launch <acct>
    --force` with NO --lane, on an account whose only live lane is LOCKED, must
    JOIN that locked lane (deliberate co-tenancy — same dir, same login family,
    no second mount), NOT fall through and mint a fresh slot on the same family
    (the GH #104 double-book). --force here means "co-tenant the locked lane on
    purpose", mirroring how `--lane <locked> --force` already joins.

    This test is the regression guard for C1: WITHOUT the `locked = set() if
    force` carve-out, force skips the locked lane, falls through, and (force
    also bypassing the #104 guard) `acquire_slot` mints a NEW slot on alpha's
    family — so the `slot_name == "slot-2"` assertion below FAILS. Confirmed by
    temporarily reverting C1 during development."""
    env = _Env()
    try:
        # alpha is live ONLY on the locked lane slot-2 (its sole occupied lane).
        _seed_live_lane("slot-2", "alpha")
        state = cus.load_state()
        state["slots"] = {"slot-2": {"account": "alpha"}}
        cus.save_state(state)
        live = {str(cus.slot_path("slot-2"))}  # shared mount NOT live
        cus.mount_pids = lambda mount: [1] if str(mount) in live else []
        cus._OCCUPIED_SLOTS_CACHE.clear()

        config = cus.deep_merge(cus.load_config(), {
            "per_session": {"lane_sharing": True},
            "session_locks": {"locked_slots": ["slot-2"]},
        })
        slot_name, slot_dir, account = cus._launch_prepare(
            "alpha", cus.load_state(), config, force=True)
        assert slot_name == "slot-2", \
            f"--force must JOIN the locked lane, got {slot_name} (a fresh mint = the #104 double-book)"
        assert account == "alpha"
        assert slot_dir == cus.slot_path("slot-2")
    finally:
        env.restore()


def test_pick_launch_account_keeps_shared_active_on_locked_lane():
    """Issue #219 / PR #220 dual review (C5b — the `_shared_active` carve-out).
    An account that is the LIVE shared-mount active is joinable via the bare
    ~/.claude mount (which can NOT be locked), so it must stay lane-share-joinable
    even when its ONLY slot lane is locked. Every other #219 test keeps CLAUDE_DIR
    NOT live, so `_shared_active` is always None and this branch never fired.

    Non-vacuity: WITHOUT the `_acct == _shared_active: continue` carve-out, gamma
    (live on locked slot-3, all-locked) would be dropped from lane_share_joinable
    and the lane-share fallback would pick alpha (5h 10%) instead of gamma (5%)."""
    env = _Env()
    try:
        state = cus.load_state()  # active == "gamma"
        # All three accounts live (saturated pool → lane-share fallback fires);
        # gamma is BOTH the shared-mount active AND live on the locked slot-3.
        state["slots"] = {
            "slot-1": {"account": "alpha"},
            "slot-2": {"account": "beta"},
            "slot-3": {"account": "gamma"},
        }
        for s in ("slot-1", "slot-2", "slot-3"):
            cus.slot_path(s).mkdir(parents=True, exist_ok=True)
        live = {str(cus.slot_path(s)) for s in ("slot-1", "slot-2", "slot-3")}
        live.add(str(cus.CLAUDE_DIR))  # shared mount IS live on gamma
        cus.mount_pids = lambda mount: [1] if str(mount) in live else []
        cus._OCCUPIED_SLOTS_CACHE.clear()

        config = cus.deep_merge(cus.load_config(), {
            "per_session": {"lane_sharing": True},
            "session_locks": {"locked_slots": ["slot-3"]},  # gamma's only slot lane
        })
        t = cus.pick_launch_account(state, config)
        # gamma stays joinable (via the shared mount) and is lowest-usage (5%).
        assert t is not None and t.name == "gamma", t
        assert "lane-share fallback" in t.reason
    finally:
        env.restore()


def test_launch_prepare_auto_joins_nonlocked_lane_end_to_end():
    """Issue #219 / PR #220 dual review (C5c — end-to-end `auto` pick→join). The
    two #219 halves (picker-skip in pick_launch_account, join-skip in
    _launch_prepare) are each tested in isolation; this drives the FULL auto flow
    through `_launch_prepare("auto", ...)` to assert their composition — pick a
    lane-share-joinable account, then JOIN its NON-locked lane, skipping a locked
    lane on the same account."""
    env = _Env()
    try:
        # Saturate the pool so the auto pick reaches the lane-share fallback:
        # alpha live on [locked slot-2, unlocked slot-5], beta live on slot-1,
        # gamma dropped + shared mount NOT live (no idle account to spread onto).
        _seed_live_lane("slot-5", "alpha")  # the healthy lane we expect to JOIN
        cus.slot_path("slot-2").mkdir(parents=True, exist_ok=True)  # locked, live
        cus.slot_path("slot-1").mkdir(parents=True, exist_ok=True)  # beta, live
        state = cus.load_state()
        state["accounts"].pop("gamma", None)
        state["slots"] = {
            "slot-1": {"account": "beta"},
            "slot-2": {"account": "alpha"},
            "slot-5": {"account": "alpha"},
        }
        cus.save_state(state)
        live = {str(cus.slot_path(s)) for s in ("slot-1", "slot-2", "slot-5")}  # shared NOT live
        cus.mount_pids = lambda mount: [1] if str(mount) in live else []
        cus._OCCUPIED_SLOTS_CACHE.clear()

        # Degrade the fresh-reading verify-poll to "no trustworthy reading" so the
        # auto pick is trusted as-is with no network call (the loop's own
        # documented safe-degrade path), keeping the test deterministic.
        _saved_poll = cus._force_poll_launch_candidate
        cus._force_poll_launch_candidate = lambda *a, **k: False
        try:
            config = cus.deep_merge(cus.load_config(), {
                "per_session": {"lane_sharing": True},
                "session_locks": {"locked_slots": ["slot-2"]},
            })
            slot_name, slot_dir, account = cus._launch_prepare("auto", cus.load_state(), config)
        finally:
            cus._force_poll_launch_candidate = _saved_poll

        # alpha (5h 10%) is the lowest-usage joinable account; its join skips the
        # LOCKED slot-2 and lands on the unlocked slot-5.
        assert account == "alpha", f"auto should pick lane-share-joinable alpha, got {account}"
        assert slot_name == "slot-5", f"join must skip locked slot-2 for slot-5, got {slot_name}"
        assert slot_dir == cus.slot_path("slot-5")
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
