"""Plumbing / launch / SOS / reactive tests for the capacity-aware anti-herding
rollout (Phase 2b / Task 5; docs/plans/2026-07-10-capacity-aware-anti-herding.md).

Task 4 covered the scorer + decide_swap conversions in isolation
(tests/test_capacity_gate_conversions.py). This file covers the SITES Task 5
wires the ctx into: the launch picker's raw fallbacks (G10) and its verify-and-
repick wall (G3/G9), the SOS target-side labeller and source-side rotation
probe (G8), and the per_session reactive escape path. Every assertion pins a
gate-ON reference-units decision against the gate-OFF raw-percent one so the
conversion (not just "it runs") is what's tested.

Run standalone:  python3 -m pytest tests/test_capacity_plumbing.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402
from capacity_fixtures import _acct, cap_config, cap_ctx  # noqa: E402


def _cap_config_with_accounts(names, *, reference_x=5, **overrides):
    """cap_config plus an `accounts:` list so the fleet-level `_capacity_ctx`
    (which enumerates config accounts, not state accounts) sees them. Per-account
    capacity_x is supplied via the state's cached field in each test."""
    cfg = cap_config(reference_x=reference_x, **overrides)
    cfg = cus.deep_merge(cfg, {"accounts": [{"name": n} for n in names]})
    return cfg


# ---------------------------------------------------------------------------
# G10 — launch picker raw fallbacks pick by per-lane reference units.
# ---------------------------------------------------------------------------

def test_g10_lane_share_fallback_picks_loaded_big_tier(monkeypatch):
    """Saturated fleet: every healthy account is on a live mount, so
    pick_launch_account falls to the lane-share fallback (G10). Gate-on it picks
    the account with the most per-lane REMAINING reference units — a loaded
    20x@80% (0.8u) beats a lower-percent 5x@50% (0.5u). Gate-off the same
    fallback picks the lowest raw estimated percent (the 5x@50%)."""
    accounts = {
        "small5x": _acct(50.0, 10.0, capacity_x=5),
        "big20x": _acct(80.0, 10.0, capacity_x=20),
    }
    # Force the lane-share branch: both accounts are live-mounted (so `_try`
    # excludes them and the first raw fallback's "not live" pool is empty).
    monkeypatch.setattr(cus, "mount_in_use", lambda d: False)
    monkeypatch.setattr(cus, "_live_slot_accounts", lambda state: {"small5x", "big20x"})
    monkeypatch.setattr(cus, "occupied_slot_accounts",
                        lambda state, **k: {"small5x": ["slot-1"], "big20x": ["slot-2"]})
    cus._OCCUPIED_SLOTS_CACHE.clear()

    cfg_on = _cap_config_with_accounts(["small5x", "big20x"],
                                       per_session={"lane_sharing": True})
    state = {"active": None, "slots": {},
             "accounts": {k: dict(v) for k, v in accounts.items()}}
    picked_on = cus.pick_launch_account(state, cfg_on)
    assert picked_on is not None and picked_on.name == "big20x"

    cfg_off = cus.deep_merge(cfg_on, {"capacity_aware": {"enabled": False}})
    picked_off = cus.pick_launch_account(state, cfg_off)
    assert picked_off is not None and picked_off.name == "small5x"


# ---------------------------------------------------------------------------
# Launch verify-and-repick wall (_launch_candidate_saturated: G3 + G9).
# ---------------------------------------------------------------------------

def test_launch_verify_20x_at_80_not_rejected_gate_on():
    """The fresh-reading re-check (_launch_candidate_saturated) agrees with the
    picker: a freshly-polled idle 20x@80% has 0.8u > the 0.5u health line, so
    gate-on it is NOT walled (G3/formula 2). Gate-off the raw 80% ≥ steps[0]=50
    ladder step still walls it — the pre-fix behavior."""
    acct = _acct(80.0, 10.0, next_swap_at_pct=50)
    ctx = cap_ctx(5, {"big20x": 20}, {})
    cfg_on = cap_config()

    sat_on, why_on = cus._launch_candidate_saturated(acct, cfg_on, name="big20x", ctx=ctx)
    assert not sat_on, f"gate-on should accept a 0.8u 20x; got saturated: {why_on!r}"

    sat_off, why_off = cus._launch_candidate_saturated(acct, cap_config(enabled=False))
    assert sat_off and "step" in why_off  # raw 80% ≥ 50% ladder step


# ---------------------------------------------------------------------------
# SOS target-side (G8): the loss-reason labeller does not wall a healthy 20x.
# ---------------------------------------------------------------------------

def test_sos_target_side_20x_at_80_not_reported_saturated():
    """`_premium_target_loss_reason` (the SOS lost-capacity labeller) gains a
    ctx param. Gate-on a 20x@80% is a genuine target (0.8u > 0.5u), so it is
    labelled 'unavailable' — NOT walled at its ladder step. Gate-off (no ctx)
    the raw 80% ≥ 50% ladder step still reads it as lost capacity."""
    acct = _acct(80.0, 10.0, next_swap_at_pct=50)
    ctx = cap_ctx(5, {"big20x": 20}, {})

    reason_on = cus._premium_target_loss_reason("big20x", acct, cap_config(), ctx=ctx)
    assert reason_on == "unavailable"

    reason_off = cus._premium_target_loss_reason("big20x", acct, cap_config(enabled=False))
    assert "ladder step" in reason_off


# ---------------------------------------------------------------------------
# SOS source-side (G8): Condition 2b rotation probe uses formula 3 + the clamp.
# ---------------------------------------------------------------------------

def _diagnose_2b_conditions(state, config, occupied, monkeypatch):
    monkeypatch.setattr(cus, "occupied_slot_accounts", lambda s, **k: dict(occupied))
    cus._OCCUPIED_SLOTS_CACHE.clear()
    conds = cus.diagnose(state, config)
    return [c for c in conds if "no swap target" in c.summary]


def test_sos_source_side_20x_at_80_not_wants_to_rotate(monkeypatch):
    """Condition 2b's lane precondition ("is this lane over its step?") is
    formula 3 gate-on. A 20x@80% lane with next_swap_at=70 has 0.8u > (100−70)/100
    = 0.3u, so gate-on it does NOT want to rotate → no starvation alarm. Gate-off
    the raw 80% ≥ 70% DOES trip the probe and (with the only spare saturated)
    raises the 'no swap target' alarm."""
    accounts = {
        "big20x": _acct(80.0, 10.0, next_swap_at_pct=70, capacity_x=20),
        "spare": _acct(100.0, 10.0, next_swap_at_pct=70, capacity_x=5),  # saturated → no target
    }
    cfg_on = _cap_config_with_accounts(["big20x", "spare"], mode="per_session")
    state = {"active": None, "slots": {"slot-1": {"account": "big20x"}},
             "accounts": {k: dict(v) for k, v in accounts.items()}}

    on = _diagnose_2b_conditions(state, cfg_on, {"big20x": ["slot-1"]}, monkeypatch)
    assert not any(c.affected == "big20x" for c in on), \
        f"gate-on should not report the 0.8u lane starved: {[c.summary for c in on]}"

    cfg_off = cus.deep_merge(cfg_on, {"capacity_aware": {"enabled": False}})
    off = _diagnose_2b_conditions(state, cfg_off, {"big20x": ["slot-1"]}, monkeypatch)
    assert any(c.affected == "big20x" for c in off), \
        "gate-off raw 80% ≥ 70% with a saturated spare should raise 2b"


def test_sos_source_side_sentinel_next_swap_at_100_uses_clamped_step(monkeypatch):
    """next_swap_at=100 is the end-of-ladder sentinel; the source-side probe
    adopts the SAME ≥100→steps[-1] clamp G4 uses. A 20x@98% has 0.08u; against
    the clamped step (steps[-1]=90 → 0.10u line) 0.08u ≤ 0.10u ⇒ it WANTS to
    rotate, so gate-on the starvation alarm fires. If the sentinel were read raw
    (100 → 0.0u line) 0.08u > 0.0u would say "not over step" and suppress it —
    so a firing alarm here proves the clamp is applied."""
    accounts = {
        "big20x": _acct(98.0, 10.0, next_swap_at_pct=100, capacity_x=20),
        "spare": _acct(100.0, 10.0, next_swap_at_pct=100, capacity_x=5),
    }
    cfg = _cap_config_with_accounts(["big20x", "spare"], mode="per_session")
    state = {"active": None, "slots": {"slot-1": {"account": "big20x"}},
             "accounts": {k: dict(v) for k, v in accounts.items()}}
    fired = _diagnose_2b_conditions(state, cfg, {"big20x": ["slot-1"]}, monkeypatch)
    assert any(c.affected == "big20x" for c in fired), \
        "sentinel next_swap_at=100 must clamp to steps[-1]=90 (0.08u ≤ 0.10u ⇒ over step)"


# ---------------------------------------------------------------------------
# Reactive per_session escape path picks by reference units.
# ---------------------------------------------------------------------------

def test_reactive_per_session_picks_by_units(monkeypatch):
    """A per_session 429 on a hot lane escapes to the account with the most
    reference-unit headroom. Gate-on the loaded 20x@80% (0.8u) is chosen over
    the lower-percent 5x@40% (0.6u); gate-off the raw lowest-percent 5x wins
    (both idle accounts sit under the 50% ladder step so neither is vetoed as
    would-re-trip in either mode). Built by driving a minimal one-entry 429
    batch through check_rate_limit_reactive_per_session."""
    monkeypatch.setattr(cus, "session_current_slot", lambda sid: None)
    monkeypatch.setattr(cus, "occupied_slot_accounts", lambda s, **k: {})
    monkeypatch.setattr(cus, "_distinct_family_capacity", lambda *a, **k: 99)
    cus._OCCUPIED_SLOTS_CACHE.clear()

    def _run(gate_on):
        accounts = {
            "hot": _acct(96.0, 20.0, next_swap_at_pct=95, capacity_x=5,
                         last_swap_ts=cus.now_iso()),
            "small5x": _acct(40.0, 10.0, next_swap_at_pct=95, capacity_x=5),
            "big20x": _acct(80.0, 10.0, next_swap_at_pct=95, capacity_x=20),
        }
        # last_swap far in the past so the reactive hysteresis guard never holds.
        old = (cus.datetime.now(cus.timezone.utc)
               - cus.timedelta(seconds=100000)).isoformat().replace("+00:00", "Z")
        for a in accounts.values():
            a["last_swap_ts"] = old
        cfg = _cap_config_with_accounts(
            ["hot", "small5x", "big20x"], strategy="lowest_usage",
            reactive={"enabled": True}, swap_hysteresis={"enabled": False})
        if not gate_on:
            cfg = cus.deep_merge(cfg, {"capacity_aware": {"enabled": False}})
        state = {"active": "hot", "slots": {"slot-1": {"account": "hot"}},
                 "accounts": {k: dict(v) for k, v in accounts.items()}}
        entries = [{"session_id": "sess0001", "slot": "slot-1", "account": "hot"}]
        moves = cus.check_rate_limit_reactive_per_session(state, cfg, entries=entries)
        return moves

    moves_on = _run(True)
    assert moves_on and moves_on[0]["to"] == "big20x", moves_on

    moves_off = _run(False)
    assert moves_off and moves_off[0]["to"] == "small5x", moves_off


# ---------------------------------------------------------------------------
# `cus status` display smoke test (capacity_x + remaining-units columns).
# ---------------------------------------------------------------------------

def test_status_display_shows_capacity_columns(monkeypatch, tmp_path):
    """Gate-on, `cus status` renders a per-account tier + remaining-units column
    (display only). Smoke test: the command runs and the 20x tier + a units
    figure appear; gate-off the extra column is absent."""
    from click.testing import CliRunner

    accounts = {
        "small5x": _acct(50.0, 10.0, capacity_x=5),
        "big20x": _acct(80.0, 10.0, capacity_x=20),
    }
    state = {"active": "small5x", "accounts": {k: dict(v) for k, v in accounts.items()},
             "slots": {}}
    state_file = tmp_path / "state.json"
    cus.write_json(state_file, state)
    monkeypatch.setattr(cus, "STATE_JSON", state_file)
    monkeypatch.setattr(cus, "occupied_slot_accounts", lambda s, **k: {})

    cfg_on = _cap_config_with_accounts(["small5x", "big20x"])
    monkeypatch.setattr(cus, "load_config", lambda: cfg_on)
    runner = CliRunner()
    res_on = runner.invoke(cus.cli, ["status"])
    assert res_on.exit_code == 0, res_on.output
    assert "20x" in res_on.output and "u free" in res_on.output

    cfg_off = cus.deep_merge(cfg_on, {"capacity_aware": {"enabled": False}})
    monkeypatch.setattr(cus, "load_config", lambda: cfg_off)
    res_off = runner.invoke(cus.cli, ["status"])
    assert res_off.exit_code == 0, res_off.output
    assert "u free" not in res_off.output
