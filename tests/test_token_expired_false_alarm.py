"""Tests for the 2026-09-10 false "TOKEN EXPIRED — re-auth this account" alarm
(GH #13 follow-up).

Incident: an idle, `cus disable`d account (`default`) sat long enough that its
~1h stored ACCESS token lapsed while its REFRESH token stayed perfectly valid.
`cus force-poll default` printed "TOKEN EXPIRED — re-auth this account" and
status showed TOKEN_EXPIRED, so the daemon evicted the account's slots and the
operator did an UNNECESSARY browser relogin. The tell it was benign: the very
next poll returned 429 (the token authenticates fine — a genuinely dead token
401s with invalid_grant, it does not 429).

Root cause: poll_account_usage's HTTP-401 branch assumed the expiresAt pre-flight
had already proved the access token fresh, so a 401 there *must* be a
refresh-token-level failure = token_expired. But that pre-flight can be BYPASSED
— `_read_access_token_with_expiry`'s `_is_fresh` treats an unknown/unparseable
expiresAt as "fresh", and the int(expiresAt) parse is wrapped in a swallowing
try/except — so a benign stale access token whose refresh token is valid can
reach the 401 branch and get mislabeled.

Fix: the 401 branch now keys off refresh-token RECOVERABILITY
(`_account_has_recoverable_refresh_token`), not the access token's expiry. A 401
whose account still holds a not-known-dead refresh token is classified as the
benign, self-healing token_stale (which the un-stale machinery then probes and
escalates only on a definitive invalid_grant). Only an ABSENT / already-known-
dead refresh token stays token_expired (real browser relogin).

Run standalone:  python3 tests/test_token_expired_false_alarm.py
Or under pytest: pytest tests/test_token_expired_false_alarm.py
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture: repoint cus's path constants at a throwaway tree (mirrors the style
# in test_subscription_disabled.py / test_poll_token_stale.py so this file runs
# standalone without pytest fixtures).
# ---------------------------------------------------------------------------

class _Env:
    def __init__(self, state: dict, accounts_creds: dict[str, dict] | None = None,
                 mount_creds: dict | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.accounts_dir = root / "claude-accounts"
        self.accounts_dir.mkdir()
        self.state_json = self.accounts_dir / "state.json"
        self.state_json.write_text(json.dumps(state))
        self.config_yaml = self.accounts_dir / "config.yaml"
        for name, creds in (accounts_creds or {}).items():
            d = self.accounts_dir / f"account-{name}"
            d.mkdir()
            (d / ".credentials.json").write_text(json.dumps(creds))

        claude_dir = root / ".claude"
        claude_dir.mkdir()
        self.creds_json = claude_dir / ".credentials.json"
        if mount_creds is not None:
            self.creds_json.write_text(json.dumps(mount_creds))

        self._saved = {k: getattr(cus, k) for k in
                       ("STATE_JSON", "CONFIG_YAML", "ACCOUNTS_DIR", "HOME",
                        "CLAUDE_DIR", "CREDS_JSON")}
        cus.STATE_JSON = self.state_json
        cus.CONFIG_YAML = self.config_yaml
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.HOME = root
        cus.CLAUDE_DIR = claude_dir
        cus.CREDS_JSON = self.creds_json

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _oauth(access="at", refresh="rt", expires_at=2_000_000_000_000) -> dict:
    """A claudeAiOauth blob. expires_at=None omits the field entirely (the
    unknown-expiry bypass route). expires_at defaults to the far future so the
    pre-flight staleness gate is skipped and the HTTP 401 branch is reached
    directly."""
    oauth = {"accessToken": access}
    if refresh is not None:
        oauth["refreshToken"] = refresh
    if expires_at is not None:
        oauth["expiresAt"] = expires_at
    return {"claudeAiOauth": oauth}


def _state(active="other", accounts=None, slots=None) -> dict:
    return {
        "active": active,
        "accounts": accounts if accounts is not None else {"a": {}},
        "slots": slots or {},
        "swap_history": [],
    }


def _fake_401():
    """A urlopen replacement that 401s the usage endpoint and asserts the OAuth
    token endpoint is NOT touched (these tests use fresh/None expiry so the
    self-refresh pre-flight must never fire)."""
    def _urlopen(req, timeout=None):
        url = req.full_url
        if url == cus.USAGE_API_URL:
            raise urllib.error.HTTPError(
                url, 401, "Unauthorized", {},
                io.BytesIO(b'{"error":"unauthorized"}'))
        if url == cus.OAUTH_TOKEN_URL:
            raise AssertionError("pre-flight fired an OAuth grant unexpectedly")
        raise AssertionError(f"unexpected URL {url}")
    return _urlopen


def _fake_429():
    def _urlopen(req, timeout=None):
        url = req.full_url
        if url == cus.USAGE_API_URL:
            raise urllib.error.HTTPError(
                url, 429, "Too Many Requests", {},
                io.BytesIO(b'{"error":{"type":"rate_limit_error"}}'))
        # subscription_guard may probe the profile endpoint on a 429; that path
        # is exercised elsewhere. Fail open here (treated as "unknown").
        raise urllib.error.URLError("probe skipped in this test")
    return _urlopen


# ---------------------------------------------------------------------------
# (1) The read-only discriminator helper.
# ---------------------------------------------------------------------------

def test_helper_true_when_canonical_has_refresh():
    env = _Env(_state(accounts={"a": {}}), accounts_creds={"a": _oauth()})
    try:
        assert cus._account_has_recoverable_refresh_token("a") is True
    finally:
        env.restore()


def test_helper_false_when_no_refresh_token():
    env = _Env(_state(accounts={"a": {}}),
               accounts_creds={"a": _oauth(refresh=None)})
    try:
        assert cus._account_has_recoverable_refresh_token("a") is False
    finally:
        env.restore()


def test_helper_false_when_no_creds_at_all():
    env = _Env(_state(accounts={"a": {}}))  # no creds files written
    try:
        assert cus._account_has_recoverable_refresh_token("a") is False
    finally:
        env.restore()


def test_helper_false_when_known_dead_even_if_refresh_present():
    """A prior invalid_grant probe set snapshot_refresh_dead — honor it so a
    real relogin need is not masked."""
    env = _Env(_state(accounts={"a": {"snapshot_refresh_dead": True}}),
               accounts_creds={"a": _oauth()})
    try:
        assert cus._account_has_recoverable_refresh_token("a") is False
    finally:
        env.restore()


def test_helper_true_via_live_mount_when_active():
    """Active account with no canonical file but a refresh token on the live
    shared mount (which Claude Code refreshes transparently) is recoverable."""
    env = _Env(_state(active="a", accounts={"a": {}}), mount_creds=_oauth())
    try:
        assert cus._account_has_recoverable_refresh_token("a") is True
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (2) poll_account_usage 401 classification — the core fix.
# ---------------------------------------------------------------------------

def test_401_with_valid_refresh_is_token_stale_not_expired(monkeypatch):
    """THE fix: a 401 whose account still holds a valid refresh token is the
    benign self-healing token_stale, NOT the browser-relogin token_expired."""
    env = _Env(_state(accounts={"a": {}}), accounts_creds={"a": _oauth()})
    try:
        monkeypatch.setattr(cus.urllib.request, "urlopen", _fake_401())
        u = cus.poll_account_usage("a")
        assert u.token_stale is True
        assert u.token_expired is False
    finally:
        env.restore()


def test_401_none_expiry_bypass_is_token_stale(monkeypatch):
    """Reproduces the actual pre-flight-bypass root cause: an access token with
    NO stored expiresAt slips past the staleness gate (_is_fresh treats unknown
    as fresh), 401s on the live call, and — with a valid refresh token — must be
    token_stale, not the false token_expired."""
    env = _Env(_state(accounts={"a": {}}),
               accounts_creds={"a": _oauth(expires_at=None)})
    try:
        monkeypatch.setattr(cus.urllib.request, "urlopen", _fake_401())
        u = cus.poll_account_usage("a")
        assert u.token_stale is True
        assert u.token_expired is False
    finally:
        env.restore()


def test_401_without_refresh_is_real_token_expired(monkeypatch):
    """No usable refresh token → self-healing is impossible → a 401 is a REAL
    relogin condition and must stay token_expired (preserves GH #13 detection)."""
    env = _Env(_state(accounts={"a": {}}),
               accounts_creds={"a": _oauth(refresh=None)})
    try:
        monkeypatch.setattr(cus.urllib.request, "urlopen", _fake_401())
        u = cus.poll_account_usage("a")
        assert u.token_expired is True
        assert u.token_stale is False
    finally:
        env.restore()


def test_401_with_known_dead_refresh_is_token_expired(monkeypatch):
    """A refresh token present on disk but already proven dead (invalid_grant →
    snapshot_refresh_dead) must NOT be relabeled benign — it needs a relogin."""
    env = _Env(_state(accounts={"a": {"snapshot_refresh_dead": True}}),
               accounts_creds={"a": _oauth()})
    try:
        monkeypatch.setattr(cus.urllib.request, "urlopen", _fake_401())
        u = cus.poll_account_usage("a")
        assert u.token_expired is True
        assert u.token_stale is False
    finally:
        env.restore()


def test_429_never_sets_reauth_flag(monkeypatch):
    """A 429 proves the token authenticates — it must never set token_expired
    (or token_stale). It is rate_limited only."""
    env = _Env(_state(accounts={"a": {}}), accounts_creds={"a": _oauth()})
    try:
        monkeypatch.setattr(cus.urllib.request, "urlopen", _fake_429())
        u = cus.poll_account_usage("a")
        assert u.token_expired is False
        assert u.token_stale is False
        assert u.raw.get("rate_limited") is True
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (3) The persisted (sticky) token_expired flag must self-resolve on recovery.
# ---------------------------------------------------------------------------

def test_update_state_token_stale_clears_prior_token_expired():
    """An account left flagged token_expired that later polls as token_stale
    must have the sticky flag cleared (Branch 0 of update_state_with_usage)."""
    env = _Env(_state(accounts={"a": {"token_expired": True, "current_5h_pct": 10}}),
               accounts_creds={"a": _oauth()})
    try:
        state = cus.load_state()
        stale = cus.AccountUsage.empty()
        stale.token_stale = True
        stale.raw = {"error": "stored access token expired (refresh token still valid)"}
        cus.update_state_with_usage(state, {"a": stale})
        assert state["accounts"]["a"].get("token_expired") is False
        assert state["accounts"]["a"].get("token_stale") is True
    finally:
        env.restore()


def test_update_state_429_clears_prior_token_expired():
    """A 429 recovery clears a lingering token_expired flag (Branch 2)."""
    env = _Env(_state(accounts={"a": {"token_expired": True, "current_5h_pct": 10}}),
               accounts_creds={"a": _oauth()})
    try:
        state = cus.load_state()
        rl = cus.AccountUsage.empty()
        rl.raw = {"error": "HTTP 429 (rate_limited)", "rate_limited": True}
        cus.update_state_with_usage(state, {"a": rl})
        assert state["accounts"]["a"].get("token_expired") is False
        assert state["accounts"]["a"].get("rate_limited") is True
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# Standalone runner (mirrors the other test files). Provides a tiny monkeypatch
# shim so `python3 tests/test_token_expired_false_alarm.py` works without pytest.
# ---------------------------------------------------------------------------

class _MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        old = getattr(target, name)
        self._undo.append((target, name, old))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            setattr(target, name, old)
        self._undo.clear()


def _run_all() -> int:
    import inspect
    import types
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
    failed = 0
    for t in tests:
        mp = _MonkeyPatch()
        try:
            if "monkeypatch" in inspect.signature(t).parameters:
                t(mp)
            else:
                t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
        finally:
            mp.undo()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
