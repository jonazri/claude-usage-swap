"""Table-driven tests for the capacity-aware Normalization model's Phase 1
sourcing helpers (capacity-aware spec 2026-07-10, task brief:
.superpowers/sdd/task-3-brief.md).

Covers:
  - `_read_rate_limit_tier`'s regex parse and freshest-wins credential chain
    (shared with `_read_access_token_with_expiry` via `_account_creds_candidates`).
  - `_account_capacity_x` / `_account_raw_capacity_x`'s config-override / parse
    / neutral precedence and poll-time cache refresh.
  - `_observed_fleet_min_x`'s enabled-only, known-tier-only domain.
  - `_resolve_reference_x`'s pin / snapshot / compute-and-persist precedence
    and snapshot stickiness across resolutions.
  - `_capacity_ctx`'s eager cache fill vs. trusting an already-cached value.
  - `_remaining_units`'s ratio formula.
  - `_capacity_gate_on`'s default-off gate.
  - `_capacity_warnings`'s drift and sub-reference checks, incl. the
    operator-disabled INFO downgrade.

ALL new code under test is inert (Phase 1: nothing but the single poll-cycle
wiring point inside `one_cycle` calls any of it) — these tests exercise the
helpers directly, never through a daemon cycle or an existing swap-decision
path. No existing test/golden is touched by this file.

Run standalone: python3 -m pytest tests/test_capacity_sourcing.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# ---------------------------------------------------------------------------
# Credentials-file sandbox (mirrors tests/test_token_stale_observability.py's
# `_CredEnv`): redirects cus's path constants at a throwaway account tree so
# `_read_rate_limit_tier` (and anything that calls it) never touches the real
# ~/claude-accounts/ tree.
# ---------------------------------------------------------------------------

class _CredEnv:
    def __init__(self):
        self._saved = {
            "STATE_JSON": cus.STATE_JSON,
            "ACCOUNTS_DIR": cus.ACCOUNTS_DIR,
            "CREDS_JSON": cus.CREDS_JSON,
            "mount_in_use": cus.mount_in_use,
        }
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        cus.ACCOUNTS_DIR = self.root
        cus.STATE_JSON = self.root / "state.json"
        cus.CREDS_JSON = self.root / "live-shared" / ".credentials.json"
        cus.mount_in_use = lambda d: False

    def write_tier(self, account: str, raw_tier) -> Path:
        """Write account's primary credentials store. `raw_tier=None` omits
        the `rateLimitTier` key entirely (the "field missing" case)."""
        path = self.root / f"account-{account}" / ".credentials.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        oauth = {"accessToken": "tok", "expiresAt": int(time.time() * 1000) + 3_600_000}
        if raw_tier is not None:
            oauth["rateLimitTier"] = raw_tier
        path.write_text(json.dumps({"claudeAiOauth": oauth}))
        return path

    def write_slot_tier(self, slot_name: str, account: str, raw_tier, live: bool = True):
        d = self.root / slot_name
        d.mkdir(parents=True, exist_ok=True)
        (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "tok", "expiresAt": int(time.time() * 1000) + 3_600_000,
            "rateLimitTier": raw_tier,
        }}))
        if live:
            prev = cus.mount_in_use
            cus.mount_in_use = lambda p, _prev=prev: (Path(p).name == slot_name) or _prev(p)

    def write_state(self, state: dict):
        cus.STATE_JSON.write_text(json.dumps(state))

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _cfg(**overrides) -> dict:
    return cus.deep_merge(cus.DEFAULT_CONFIG, overrides)


# ---------------------------------------------------------------------------
# 1. `_read_rate_limit_tier` — regex parse table + missing/absent + chain reuse
# ---------------------------------------------------------------------------

def test_read_rate_limit_tier_parse_table():
    """suffixed `_20x_v2`, bare prefix `20x`, `tier-20x`, and the pathological
    `_0x` all parse via the shared regex; an unparseable string returns the
    raw value with parsed_x=None."""
    cases = [
        ("_20x_v2", 20),
        ("20x", 20),
        ("tier-20x", 20),
        ("_0x", 0),
        ("not-a-tier", None),
    ]
    env = _CredEnv()
    try:
        for i, (raw, expected) in enumerate(cases):
            name = f"acct{i}"
            env.write_tier(name, raw)
            parsed, got_raw = cus._read_rate_limit_tier(name)
            assert parsed == expected, f"{raw!r} -> {parsed}, want {expected}"
            assert got_raw == raw
    finally:
        env.restore()


def test_read_rate_limit_tier_missing_field_is_none_none():
    """rateLimitTier key absent entirely (older snapshot) -> (None, None)."""
    env = _CredEnv()
    try:
        env.write_tier("acct", None)
        parsed, raw = cus._read_rate_limit_tier("acct")
        assert (parsed, raw) == (None, None)
    finally:
        env.restore()


def test_read_rate_limit_tier_no_creds_file_is_none_none():
    env = _CredEnv()
    try:
        parsed, raw = cus._read_rate_limit_tier("ghost")
        assert (parsed, raw) == (None, None)
    finally:
        env.restore()


def test_read_rate_limit_tier_reuses_freshest_wins_chain():
    """A live slot's creds win over a stale-tiered primary store — the exact
    same source-precedence `_read_access_token_with_expiry` uses."""
    env = _CredEnv()
    try:
        env.write_tier("x", "5x")
        env.write_slot_tier("slot-1", "x", "20x", live=True)
        env.write_state({"active": None, "slots": {"slot-1": {"account": "x"}}})
        parsed, raw = cus._read_rate_limit_tier("x")
        assert (parsed, raw) == (20, "20x")
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# 2. `_account_capacity_x` / `_account_raw_capacity_x` — override / parse /
#    neutral precedence, invalid-override and `_0x` warnings, cache refresh.
# ---------------------------------------------------------------------------

def test_account_capacity_x_valid_override_wins_no_disk_needed():
    config = _cfg(accounts=[{"name": "a", "capacity_x": 20}])
    state = {"accounts": {}}
    value, warnings = cus._account_capacity_x("a", state, config)
    assert value == 20
    assert warnings == []
    assert state["accounts"]["a"]["capacity_x"] == 20


def test_account_capacity_x_invalid_override_falls_to_parsed_tier():
    """A non-numeric override warns and falls through to the credentials-
    parsed tier, instead of short-circuiting to neutral."""
    env = _CredEnv()
    try:
        env.write_tier("a", "20x")
        config = _cfg(accounts=[{"name": "a", "capacity_x": "twenty"}])
        state = {"accounts": {}}
        value, warnings = cus._account_capacity_x("a", state, config)
        assert value == 20
        assert len(warnings) == 1
        assert "invalid" in warnings[0]
        assert state["accounts"]["a"]["capacity_x"] == 20
    finally:
        env.restore()


def test_account_capacity_x_sub_one_override_is_invalid():
    """A numeric-but-<1 override (e.g. 0) is invalid too, not just wrong type."""
    env = _CredEnv()
    try:
        env.write_tier("a", "5x")
        config = _cfg(accounts=[{"name": "a", "capacity_x": 0}])
        state = {"accounts": {}}
        value, warnings = cus._account_capacity_x("a", state, config)
        assert value == 5
        assert len(warnings) == 1
    finally:
        env.restore()


def test_account_capacity_x_zero_parse_warns_and_goes_neutral():
    """`_0x` parses to 0, which is itself invalid (unroutable) — warning, and
    the account becomes neutral (capacity_x == reference_x, ratio 1)."""
    env = _CredEnv()
    try:
        env.write_tier("z", "_0x")
        config = _cfg(accounts=[{"name": "z"}, {"name": "known", "capacity_x": 8}],
                      capacity_aware={"reference_x": 8})
        state = {"accounts": {}}
        value, warnings = cus._account_capacity_x("z", state, config)
        assert value == 8   # == pinned reference_x -> neutral ratio 1
        assert any("invalid" in w for w in warnings)
        # Neutral fallback is NOT cached (it's derived, not observed).
        assert "capacity_x" not in state["accounts"].get("z", {})
    finally:
        env.restore()


def test_account_capacity_x_unknown_tier_is_neutral():
    """No override, no creds file at all -> neutral == resolved reference_x."""
    env = _CredEnv()
    try:
        config = _cfg(accounts=[{"name": "unknown"}], capacity_aware={"reference_x": 10})
        state = {"accounts": {}}
        value, warnings = cus._account_capacity_x("unknown", state, config)
        assert value == 10
        assert "capacity_x" not in state["accounts"].get("unknown", {})
    finally:
        env.restore()


def test_account_capacity_x_refreshes_cache_on_different_read():
    """Poll-time semantics: a fresh read that differs from the cached value
    refreshes it in place (the wiring point relies on exactly this)."""
    env = _CredEnv()
    try:
        env.write_tier("x", "5x")
        config = _cfg(accounts=[{"name": "x"}])
        state = {"accounts": {}}
        value1, _ = cus._account_capacity_x("x", state, config)
        assert value1 == 5
        assert state["accounts"]["x"]["capacity_x"] == 5

        env.write_tier("x", "20x")   # tier changed on disk (e.g. plan upgrade)
        value2, _ = cus._account_capacity_x("x", state, config)
        assert value2 == 20
        assert state["accounts"]["x"]["capacity_x"] == 20
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# 3. `_observed_fleet_min_x` — enabled-only, known-tier-only domain
# ---------------------------------------------------------------------------

def test_observed_fleet_min_x_over_known_tiers_only():
    env = _CredEnv()
    try:
        env.write_tier("small", "5x")
        env.write_tier("big", "20x")
        config = _cfg(accounts=[
            {"name": "small"},
            {"name": "big"},
            {"name": "mystery"},   # no creds file at all -> unknown, excluded
        ])
        state = {"accounts": {}}
        result = cus._observed_fleet_min_x(state, config)
        assert result == 5.0
    finally:
        env.restore()


def test_observed_fleet_min_x_excludes_disabled_accounts():
    env = _CredEnv()
    try:
        env.write_tier("tiny", "1x")
        env.write_tier("big", "20x")
        config = _cfg(accounts=[
            {"name": "tiny", "disabled": True},
            {"name": "big"},
        ])
        state = {"accounts": {}}
        result = cus._observed_fleet_min_x(state, config)
        assert result == 20.0   # tiny excluded despite being smaller
    finally:
        env.restore()


def test_observed_fleet_min_x_all_unknown_returns_none():
    env = _CredEnv()
    try:
        config = _cfg(accounts=[{"name": "a"}, {"name": "b"}])
        state = {"accounts": {}}
        result = cus._observed_fleet_min_x(state, config)
        assert result is None
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# 4. `_resolve_reference_x` — pin / snapshot / compute-and-persist precedence
# ---------------------------------------------------------------------------

def test_resolve_reference_x_valid_pin_wins():
    config = _cfg(capacity_aware={"reference_x": 7})
    state = {"accounts": {}, "capacity_reference_snapshot": 999}  # pin beats even a snapshot
    value, warnings = cus._resolve_reference_x(state, config)
    assert value == 7.0
    assert warnings == []


def test_resolve_reference_x_invalid_reference_x_goes_snapshot_path():
    """An invalid pin (non-numeric here) is treated exactly as absent: no
    existing snapshot -> computes the fleet min, persists it, and warns."""
    env = _CredEnv()
    try:
        env.write_tier("only", "5x")
        config = _cfg(accounts=[{"name": "only"}], capacity_aware={"reference_x": "bogus"})
        state = {"accounts": {}}
        value, warnings = cus._resolve_reference_x(state, config)
        assert value == 5.0
        assert state["capacity_reference_snapshot"] == 5.0
        assert any("invalid" in w for w in warnings)
        assert any("pin" in w.lower() for w in warnings)
    finally:
        env.restore()


def test_resolve_reference_x_all_unknown_fleet_snapshots_one():
    env = _CredEnv()
    try:
        config = _cfg(accounts=[{"name": "a"}, {"name": "b"}])
        state = {"accounts": {}}
        value, warnings = cus._resolve_reference_x(state, config)
        assert value == 1.0
        assert state["capacity_reference_snapshot"] == 1.0
        assert any("pin" in w.lower() for w in warnings)
    finally:
        env.restore()


def test_resolve_reference_x_snapshot_persists_across_two_resolutions():
    """Once bootstrapped, `reference_x` stays STABLE even if a later
    resolution's fleet composition would compute a different minimum — the
    round-2 anti-instability design (removing the smallest account must not
    silently rescale everything)."""
    env = _CredEnv()
    try:
        env.write_tier("a", "10x")
        env.write_tier("b", "20x")
        config = _cfg(accounts=[{"name": "a"}, {"name": "b"}])
        state = {"accounts": {}}

        value1, warnings1 = cus._resolve_reference_x(state, config)
        assert value1 == 10.0
        assert any("pin" in w.lower() for w in warnings1)

        # Fleet composition "changes" (smallest account's tier bumped way up) —
        # a naive recompute would now see min=20, but the snapshot must win.
        env.write_tier("a", "50x")
        value2, warnings2 = cus._resolve_reference_x(state, config)
        assert value2 == 10.0          # unchanged — sticky snapshot
        assert warnings2 == []         # silent on the second resolution
    finally:
        env.restore()


def test_resolve_reference_x_present_snapshot_is_silent():
    config = _cfg()   # no pin at all
    state = {"accounts": {}, "capacity_reference_snapshot": 12}
    value, warnings = cus._resolve_reference_x(state, config)
    assert value == 12.0
    assert warnings == []


# ---------------------------------------------------------------------------
# 5. `_capacity_ctx` — eager cache fill vs. trusting an already-cached value
# ---------------------------------------------------------------------------

def test_capacity_ctx_eager_read_fills_missing_cache():
    env = _CredEnv()
    try:
        env.write_tier("y", "20x")
        config = _cfg(accounts=[{"name": "y"}], capacity_aware={"reference_x": 5})
        state = {"accounts": {}}
        ctx = cus._capacity_ctx(state, config)
        assert ctx["capacity_x_by_name"]["y"] == 20
        assert state["accounts"]["y"]["capacity_x"] == 20   # cache side-effect
    finally:
        env.restore()


def test_capacity_ctx_trusts_existing_cache_without_rereading_disk():
    env = _CredEnv()
    try:
        # No creds file at all for "x" — if _capacity_ctx tried a live read it
        # would resolve to neutral, not the pre-cached 7.
        config = _cfg(accounts=[{"name": "x"}], capacity_aware={"reference_x": 5})
        state = {"accounts": {"x": {"capacity_x": 7}}}
        ctx = cus._capacity_ctx(state, config)
        assert ctx["capacity_x_by_name"]["x"] == 7
    finally:
        env.restore()


def test_capacity_ctx_disabled_uncached_account_is_absent():
    env = _CredEnv()
    try:
        config = _cfg(accounts=[{"name": "off", "disabled": True}],
                      capacity_aware={"reference_x": 5})
        state = {"accounts": {}}
        ctx = cus._capacity_ctx(state, config)
        assert "off" not in ctx["capacity_x_by_name"]
    finally:
        env.restore()


def test_capacity_ctx_lane_load_matches_occupied_slot_accounts():
    env = _CredEnv()
    try:
        config = _cfg(accounts=[{"name": "a"}], capacity_aware={"reference_x": 5})
        d = env.root / "slot-1"
        d.mkdir(parents=True)
        (d / ".credentials.json").write_text("{}")
        cus.mount_in_use = lambda p: True
        state = {"accounts": {"a": {"capacity_x": 5}},
                 "slots": {"slot-1": {"account": "a"}}}
        ctx = cus._capacity_ctx(state, config)
        assert ctx["lane_load_by_name"] == {"a": 1}
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# 6. `_remaining_units` — ratio formula
# ---------------------------------------------------------------------------

def test_remaining_units_ratio_formula():
    ctx = {"reference_x": 5.0, "capacity_x_by_name": {"big": 20}}
    # 20x account at 80%: ratio 4, (100-80)/100 * 4 == 0.8
    assert cus._remaining_units(80.0, "big", ctx, cus.DEFAULT_CONFIG) == 0.8


def test_remaining_units_missing_name_falls_back_to_reference_ratio_one():
    ctx = {"reference_x": 5.0, "capacity_x_by_name": {}}
    # ratio 1 for an account ctx never populated
    assert cus._remaining_units(60.0, "spare", ctx, cus.DEFAULT_CONFIG) == 0.4


# ---------------------------------------------------------------------------
# 7. `_capacity_gate_on`
# ---------------------------------------------------------------------------

def test_capacity_gate_on_default_off():
    assert cus._capacity_gate_on(cus.DEFAULT_CONFIG) is False
    assert cus._capacity_gate_on(_cfg()) is False


def test_capacity_gate_on_explicit_true_false():
    assert cus._capacity_gate_on(_cfg(capacity_aware={"enabled": True})) is True
    assert cus._capacity_gate_on(_cfg(capacity_aware={"enabled": False})) is False


# ---------------------------------------------------------------------------
# 8. `_capacity_warnings` — drift direction rules + sub-reference + INFO downgrade
# ---------------------------------------------------------------------------

def test_capacity_warnings_drift_fires_when_observed_min_below_pin():
    env = _CredEnv()
    try:
        env.write_tier("small", "5x")
        config = _cfg(accounts=[{"name": "small"}], capacity_aware={"reference_x": 20})
        state = {"accounts": {}}
        warnings = cus._capacity_warnings(state, config)
        assert any("drift" in w.lower() or "BELOW" in w for w in warnings)
    finally:
        env.restore()


def test_capacity_warnings_drift_silent_when_observed_min_at_or_above_pin():
    env = _CredEnv()
    try:
        env.write_tier("acct", "20x")
        config = _cfg(accounts=[{"name": "acct"}], capacity_aware={"reference_x": 20})
        state = {"accounts": {}}
        warnings = cus._capacity_warnings(state, config)
        assert not any("drift" in w.lower() or "BELOW" in w for w in warnings)
    finally:
        env.restore()


def test_capacity_warnings_drift_silent_when_fleet_all_unknown():
    env = _CredEnv()
    try:
        config = _cfg(accounts=[{"name": "a"}], capacity_aware={"reference_x": 20})
        state = {"accounts": {}}
        warnings = cus._capacity_warnings(state, config)
        assert not any("drift" in w.lower() or "BELOW" in w for w in warnings)
    finally:
        env.restore()


def test_capacity_warnings_drift_silent_when_unpinned():
    """No valid pin at all (only a snapshot) -> nothing to "drift" from."""
    env = _CredEnv()
    try:
        env.write_tier("small", "1x")
        config = _cfg(accounts=[{"name": "small"}])
        state = {"accounts": {}, "capacity_reference_snapshot": 20}
        warnings = cus._capacity_warnings(state, config)
        assert not any("drift" in w.lower() or "BELOW" in w for w in warnings)
    finally:
        env.restore()


def test_capacity_warnings_sub_reference_fires_and_is_warn_and_run():
    env = _CredEnv()
    try:
        env.write_tier("small", "5x")
        config = _cfg(accounts=[{"name": "small"}], capacity_aware={"reference_x": 20},
                      thresholds={"steps": [50, 75, 90]})
        state = {"accounts": {}}
        # ratio 5/20 = 0.25 <= (100-50)/100 = 0.5 -> fires
        warnings = cus._capacity_warnings(state, config)
        matches = [w for w in warnings if "small" in w]
        assert len(matches) == 1
        assert not matches[0].startswith("INFO:")
    finally:
        env.restore()


def test_capacity_warnings_sub_reference_downgraded_to_info_for_disabled():
    env = _CredEnv()
    try:
        env.write_tier("small", "5x")
        config = _cfg(accounts=[{"name": "small", "disabled": True}],
                      capacity_aware={"reference_x": 20})
        state = {"accounts": {}}
        warnings = cus._capacity_warnings(state, config)
        matches = [w for w in warnings if "small" in w]
        assert len(matches) == 1
        assert matches[0].startswith("INFO:")
    finally:
        env.restore()


def test_capacity_warnings_no_sub_reference_warning_above_threshold():
    env = _CredEnv()
    try:
        env.write_tier("normal", "20x")
        config = _cfg(accounts=[{"name": "normal"}], capacity_aware={"reference_x": 20})
        state = {"accounts": {}}
        warnings = cus._capacity_warnings(state, config)
        assert not any("normal" in w for w in warnings)
    finally:
        env.restore()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
