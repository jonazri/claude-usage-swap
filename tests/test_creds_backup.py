"""Tests for GH #79 — pre-overwrite backup rotation for credential files.

The problem: each account's OAuth payload (incl. the ~30-day refresh token)
lives in exactly ONE authoritative place at any moment, so every overwrite of
a `.credentials.json` is potentially the destruction of the only copy — the
terminal event of the whole clobber bug class (#3, #70, #75, #76, #77).

The fix: `backup_credentials_file()` is called immediately before ANY write
that replaces an existing credentials file (swap save-back, owner-heal, live
install, `init --force` re-import, and restores themselves). It keeps a
bounded rotation of timestamped `.bak` generations, and
`cus restore-creds <account>` brings one back.

Run standalone:  python3 tests/test_creds_backup.py
Or under pytest: pytest tests/test_creds_backup.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _creds(refresh: str, access: str, expires_at: int = 9999999999999) -> dict:
    return {"claudeAiOauth": {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": expires_at,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }}


class _Env:
    """Throwaway on-disk account tree with every cus path constant repointed
    at it, so execute_swap / restore run for real without touching the live
    machine. Same setattr + restore() pattern as test_creds_saveback_drift."""

    def __init__(self, accounts: dict[str, dict], active: str, live_creds: dict | bytes):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        accounts_dir = root / "claude-accounts"
        claude_dir = root / ".claude"
        claude_dir.mkdir(parents=True)
        accounts_dir.mkdir(parents=True)

        self.claude_dir = claude_dir
        self.creds_json = claude_dir / ".credentials.json"
        raw = live_creds if isinstance(live_creds, bytes) else json.dumps(live_creds).encode()
        self.creds_json.write_bytes(raw)
        self.claude_json = root / ".claude.json"
        self.claude_json.write_text(json.dumps({
            "userID": f"uid-{active}", "oauthAccount": {"emailAddress": f"{active}@x"},
        }))

        self.accounts_dir = accounts_dir
        for name, creds in accounts.items():
            d = accounts_dir / f"account-{name}"
            d.mkdir()
            (d / ".credentials.json").write_text(json.dumps(creds))
            (d / ".claude.json").write_text(json.dumps({
                "userID": f"uid-{name}", "oauthAccount": {"emailAddress": f"{name}@x"},
            }))

        self.state_json = accounts_dir / "state.json"
        self.state_json.write_text(json.dumps({
            "active": active,
            "accounts": {n: {"next_swap_at_pct": 50} for n in accounts},
            "swap_history": [],
        }))
        self.inbox_md = accounts_dir / "inbox.md"

        self._saved = {k: getattr(cus, k) for k in (
            "ACCOUNTS_DIR", "STATE_JSON", "CREDS_JSON", "CLAUDE_JSON",
            "CONFIG_YAML", "INBOX_MD", "migrate_account_dir",
        )}
        cus.ACCOUNTS_DIR = accounts_dir
        cus.STATE_JSON = self.state_json
        cus.CREDS_JSON = self.creds_json
        cus.CLAUDE_JSON = self.claude_json
        cus.CONFIG_YAML = accounts_dir / "config.yaml"   # absent → pure defaults
        cus.INBOX_MD = self.inbox_md
        cus.migrate_account_dir = lambda d: {"action": "already_migrated"}

    def snapshot_path(self, name: str) -> Path:
        return self.accounts_dir / f"account-{name}" / ".credentials.json"

    def snapshot(self, name: str) -> dict:
        return json.loads(self.snapshot_path(name).read_text())

    def backups(self, name: str) -> list[Path]:
        return sorted((self.accounts_dir / f"account-{name}").glob(".credentials.json.bak.*"))

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# backup_credentials_file unit tests
# ---------------------------------------------------------------------------

def test_no_backup_when_file_missing():
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / ".credentials.json"
        assert cus.backup_credentials_file(missing) is None
        assert list(Path(tmp).iterdir()) == []


def test_backup_preserves_content_and_mode():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / ".credentials.json"
        f.write_text(json.dumps(_creds("rt-x", "at-x")))
        bak = cus.backup_credentials_file(f)
        assert bak is not None and bak.exists()
        assert bak.read_bytes() == f.read_bytes()
        assert (bak.stat().st_mode & 0o777) == 0o600
        assert bak.name.startswith(".credentials.json.bak.")


def test_rotation_bound_holds():
    """More overwrites than the keep bound → only the newest `keep`
    generations survive, and the newest backup holds the most recent
    pre-overwrite content."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / ".credentials.json"
        for i in range(cus.CREDS_BACKUP_KEEP + 3):   # 8 overwrites with keep=5
            f.write_text(json.dumps(_creds(f"rt-{i}", f"at-{i}")))
            cus.backup_credentials_file(f)
        baks = sorted(Path(tmp).glob(".credentials.json.bak.*"))
        assert len(baks) == cus.CREDS_BACKUP_KEEP
        newest = json.loads(baks[-1].read_text())
        last_i = cus.CREDS_BACKUP_KEEP + 2
        assert newest["claudeAiOauth"]["refreshToken"] == f"rt-{last_i}"
        # Oldest surviving generation is (last_i - keep + 1), older ones pruned
        oldest = json.loads(baks[0].read_text())
        assert oldest["claudeAiOauth"]["refreshToken"] == f"rt-{last_i - cus.CREDS_BACKUP_KEEP + 1}"


# ---------------------------------------------------------------------------
# execute_swap integration: backups created at the real overwrite points
# ---------------------------------------------------------------------------

def test_saveback_overwrite_creates_backup_of_outgoing_snapshot():
    """Normal a→b swap: a's pre-swap snapshot content must survive as a
    backup generation in account-a/ (the save-back replaced it)."""
    env = _Env({"a": _creds("rt-a", "at-a-old"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a", "at-a-REFRESHED"))
    try:
        cus.execute_swap("b")
        baks = env.backups("a")
        assert len(baks) == 1
        preserved = json.loads(baks[0].read_text())
        assert preserved["claudeAiOauth"]["accessToken"] == "at-a-old"
        # and the snapshot itself was updated by the save-back as before
        assert env.snapshot("a")["claudeAiOauth"]["accessToken"] == "at-a-REFRESHED"
    finally:
        env.restore()


def test_live_install_creates_backup_of_live_file():
    """The live ~/.claude/.credentials.json is also backed up before the
    target's creds are installed over it."""
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a", "at-a-LIVE"))
    try:
        cus.execute_swap("b")
        live_baks = sorted(env.claude_dir.glob(".credentials.json.bak.*"))
        assert len(live_baks) == 1
        assert json.loads(live_baks[0].read_text())["claudeAiOauth"]["accessToken"] == "at-a-LIVE"
    finally:
        env.restore()


def test_drift_heal_backs_up_owner_snapshot():
    """GH #3 foreign verdict: the owner-heal write into b's snapshot is
    preceded by a backup of b's previous snapshot content."""
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b-old")},
               active="a", live_creds=_creds("rt-b", "at-b-REFRESHED"))
    try:
        cus.execute_swap("b")
        baks = env.backups("b")
        assert len(baks) == 1
        assert json.loads(baks[0].read_text())["claudeAiOauth"]["accessToken"] == "at-b-old"
        # a's snapshot untouched → no backup needed, none created
        assert env.backups("a") == []
    finally:
        env.restore()


def test_repeated_swaps_respect_rotation_bound():
    """Swap back and forth more times than the keep bound; each account's
    backup count must stay bounded."""
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a", "at-a"))
    try:
        for i in range(cus.CREDS_BACKUP_KEEP + 3):
            target = "b" if i % 2 == 0 else "a"
            cus.execute_swap(target)
        assert len(env.backups("a")) <= cus.CREDS_BACKUP_KEEP
        assert len(env.backups("b")) <= cus.CREDS_BACKUP_KEEP
        assert len(list(env.claude_dir.glob(".credentials.json.bak.*"))) <= cus.CREDS_BACKUP_KEEP
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# restore round-trip
# ---------------------------------------------------------------------------

def test_restore_round_trips_clobbered_snapshot():
    """The GH #79 headline scenario: a snapshot gets overwritten (here by a
    legitimate save-back, but the mechanics are identical for a clobber bug);
    restore-creds brings the previous generation back byte-for-byte."""
    original = _creds("rt-a-ORIGINAL", "at-a-original")
    env = _Env({"a": original, "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a-live", "at-a-live"))
    try:
        # live carries a rotated refresh token → "unknown" verdict → save-back
        # replaces a's snapshot (backing the original up first)
        cus.execute_swap("b")
        assert env.snapshot("a")["claudeAiOauth"]["refreshToken"] == "rt-a-live"
        backups = cus.list_creds_backups("a")
        assert backups, "expected a backup generation after the save-back overwrite"
        cus.restore_creds_backup("a", backups[0])
        assert env.snapshot("a") == original
        # the restore preserved the pre-restore content as a new generation too
        contents = {b.read_text() for b in env.backups("a")}
        assert any("rt-a-live" in c for c in contents)
    finally:
        env.restore()


def test_restore_into_live_replaces_live_file():
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a-live", "at-a-live"))
    try:
        # Manufacture a backup generation for a
        cus.backup_credentials_file(env.snapshot_path("a"))
        backup = cus.list_creds_backups("a")[0]
        cus.restore_creds_backup("a", backup, into_live=True)
        live = json.loads(env.creds_json.read_text())
        assert live["claudeAiOauth"]["refreshToken"] == "rt-a"
        # the previous live content was preserved as a backup next to it
        live_baks = sorted(env.claude_dir.glob(".credentials.json.bak.*"))
        assert any("rt-a-live" in b.read_text() for b in live_baks)
    finally:
        env.restore()


def test_list_creds_backups_newest_first():
    env = _Env({"a": _creds("rt-a", "at-a")}, active="a",
               live_creds=_creds("rt-a", "at-a"))
    try:
        p = env.snapshot_path("a")
        p.write_text(json.dumps(_creds("rt-1", "at-1")))
        cus.backup_credentials_file(p)
        p.write_text(json.dumps(_creds("rt-2", "at-2")))
        cus.backup_credentials_file(p)
        backups = cus.list_creds_backups("a")
        assert len(backups) == 2
        assert json.loads(backups[0].read_text())["claudeAiOauth"]["refreshToken"] == "rt-2"
        assert json.loads(backups[1].read_text())["claudeAiOauth"]["refreshToken"] == "rt-1"
    finally:
        env.restore()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
