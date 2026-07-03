"""Tests for the per-account independent-login POOL (2026-07-03).

Plan: docs/plans/2026-07-03-independent-login-pool.md.

Phase 1 covers the store + lease primitives:
  - list_login_families / next_family_id (usable-only, numeric order)
  - leased_families: only LIVE slots' leases count (idle reclaimable)
  - free_login_family / has_free_login_family: lowest-free, exhaustion

Later phases (provisioning command, swap claim/install, decision + reactive
rescue, SOS) append here.

Run standalone:  python3 tests/test_login_pool.py
Run under pytest: pytest tests/test_login_pool.py
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
    """Throwaway on-disk tree (same monkeypatch pattern as the other suites)."""

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
        cus._OCCUPIED_SLOTS_CACHE.clear()

    def plant_family(self, account: str, family_id: str, refresh: str, usable: bool = True) -> None:
        """Write a pooled family's creds (usable=carries a refresh token)."""
        d = cus.login_family_dir(account, family_id)
        d.mkdir(parents=True, exist_ok=True)
        blob = _creds(refresh) if usable else {"claudeAiOauth": {"accessToken": "only-access"}}
        cus.login_family_creds_path(account, family_id).write_text(json.dumps(blob))

    def make_slot(self, account: str, live: bool, family_id: str | None = None) -> str:
        """Create a slot holding `account`, optionally leasing a pooled family."""
        state = cus.load_state()
        name, _ = cus.create_slot(state)
        state["slots"][name]["account"] = account
        if family_id:
            state["slots"][name]["login_family"] = f"{account}/{family_id}"
        cus.save_state(state)
        if live:
            self.live_slots.add(name)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        return name

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus._OCCUPIED_SLOTS_CACHE.clear()
        self._tmp.cleanup()


def test_list_login_families_usable_only_and_ordered():
    env = _Env()
    try:
        assert cus.list_login_families("alpha") == []  # empty pool
        env.plant_family("alpha", "family-2", "rt-a2")
        env.plant_family("alpha", "family-1", "rt-a1")
        env.plant_family("alpha", "family-10", "rt-a10")
        env.plant_family("alpha", "family-3", "bad", usable=False)  # no refresh token
        # Numeric order (not lexical: family-10 after family-2), unusable excluded.
        assert cus.list_login_families("alpha") == ["family-1", "family-2", "family-10"]
    finally:
        env.restore()


def test_next_family_id_increments_past_highest():
    env = _Env()
    try:
        assert cus.next_family_id("alpha") == "family-1"  # empty pool starts at 1
        env.plant_family("alpha", "family-1", "rt-a1")
        env.plant_family("alpha", "family-2", "rt-a2")
        assert cus.next_family_id("alpha") == "family-3"
        # A half-finished (unusable) dir still bumps the counter — don't reuse it.
        env.plant_family("alpha", "family-3", "bad", usable=False)
        assert cus.next_family_id("alpha") == "family-4"
    finally:
        env.restore()


def test_free_family_skips_live_leases_reclaims_idle():
    env = _Env()
    try:
        env.plant_family("alpha", "family-1", "rt-a1")
        env.plant_family("alpha", "family-2", "rt-a2")
        state = cus.load_state()
        assert cus.free_login_family("alpha", state) == "family-1"  # lowest free

        # A LIVE slot leasing family-1 removes it from the free set.
        env.make_slot("alpha", live=True, family_id="family-1")
        state = cus.load_state()
        assert cus.free_login_family("alpha", state) == "family-2"
        assert cus.has_free_login_family("alpha", state) is True

        # An IDLE slot leasing family-2 does NOT consume it (reclaimable).
        env.make_slot("alpha", live=False, family_id="family-2")
        state = cus.load_state()
        assert cus.free_login_family("alpha", state) == "family-2"
    finally:
        env.restore()


def test_pool_exhaustion_reports_no_free_family():
    env = _Env()
    try:
        env.plant_family("alpha", "family-1", "rt-a1")
        env.make_slot("alpha", live=True, family_id="family-1")  # only family, live-leased
        state = cus.load_state()
        assert cus.free_login_family("alpha", state) is None
        assert cus.has_free_login_family("alpha", state) is False
        # An account with no pool at all is also "no free family".
        assert cus.has_free_login_family("beta", state) is False
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
