"""Task 20 (spec-2 token-pressure STAGE 1, integration): pressure.json schema +
atomic writer.

Assembles the single artifact — level, normalized pool curves+ETAs,
per-account curves+ETAs, binding, required reduction, weight-fit confidence,
per-session table — from the EXISTING Phase-F/D building blocks
(`_pressure_triggers`/`_pressure_level`, `_required_reduction_pool`/
`_required_reduction_pinned`, `_safety_factor`), with the REAL injected
`partition` in place of the synthetic doubles those functions' own unit
tests use. `_pressure_snapshot` is PURE (no I/O); `_pressure_write_json` is
an atomic tmp+os.replace write that NEVER calls `save_state`.

HARNESS: ``python3 -m pytest tests/ -q``.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
RATIO = 4.0  # capacity_x 20 / reference_x 5

CFG = {
    "capacity_aware": {"reference_x": 5},
    "thresholds": {"steps": [70, 85, 94]},        # gate_5h = 94 (live top)
    "per_model_weekly": {"cap_pct": 95},
    "pressure": {"critical_eta_min": 60, "weekly_gate_margin_pct": 2,
                 "horizon_hours": 3, "exit_margin_hours": 1},
    "accounts": [{"name": "A", "capacity_x": 20}],
}

WEIGHT_FIT = {
    "weights": {"output": 1.0, "input": 0.1, "cache_read": 0.05,
                "cache_create_5m": 0.2, "cache_create_1h": 0.4},
    "source": "fit",
    "condition_number": 10.0,
    "residual_fraction": 0.05,
    "n_windows": 250,
}

ATTRIBUTION = {
    "confidence": 1.0,
    "blindness": False,
    "residual_fraction": 0.05,
    "reason": "steady-state: 5/5 sessions read",
}


def _iso(dt):
    return dt.isoformat()


class FakePartition:
    """Synthetic Task-11 ``_partition_burn`` double (same protocol every
    Phase-F test file uses): disjoint pinned vs rotatable burn in units/min,
    keyed by (name, window)."""

    def __init__(self, pinned=None, rotatable=None):
        self._pinned = dict(pinned or {})
        self._rotatable = dict(rotatable or {})

    def pinned_burn_units(self, name, window):
        return self._pinned.get((name, window), 0.0)

    def rotatable_burn_units(self, name, window):
        return self._rotatable.get((name, window), 0.0)


def _acct(pct=50.0, pct7d=10.0, reset_min=500, **extra):
    a = {"capacity_x": 20, "current_5h_pct": pct, "current_7d_pct": pct7d,
         "last_poll_ts": _iso(NOW)}
    if reset_min is not None:
        a["five_hour_resets_at"] = _iso(NOW + timedelta(minutes=reset_min))
    a.update(extra)
    return a


def _state(accounts):
    return {"accounts": accounts}


def _session_row(**extra):
    row = {"session_id": "s1", "account_shares": {"A": 0.01}, "model": "sonnet",
           "fable_share": 0.0, "pane": "%1", "socket": "s0", "cwd": "/x",
           "class": "interactive", "rate": 0.02, "trend": "steady",
           "coordinator_of": None}
    row.update(extra)
    return row


def _snapshot(state=None, cfg=None, partition=None, session_table=None,
             weight_fit=None, attribution=None, now=NOW, episode_id=None):
    return cus._pressure_snapshot(
        state if state is not None else _state({"A": _acct()}),
        cfg if cfg is not None else CFG,
        now,
        partition=partition if partition is not None else FakePartition(),
        session_table=session_table if session_table is not None else [_session_row()],
        weight_fit=weight_fit if weight_fit is not None else dict(WEIGHT_FIT),
        attribution=attribution if attribution is not None else dict(ATTRIBUTION),
        episode_id=episode_id,
    )


# ============================ test_schema_shape ================================

def test_schema_shape():
    """Every top-level key present; reference_x=5.0, horizon_min=180; binding
    nested with view/name; gate = live ladder top (94)/80/95."""
    snap = _snapshot()
    for key in ("level", "generated_at", "reference_x", "horizon_min", "pool",
                "accounts", "binding", "episode_id", "weight_fit",
                "safety_factor", "attribution", "sessions"):
        assert key in snap, f"missing top-level key {key!r}"

    assert snap["reference_x"] == pytest.approx(5.0)
    assert snap["horizon_min"] == pytest.approx(180.0)

    for window in ("5h", "7d"):
        pool_w = snap["pool"][window]
        for key in ("capacity_units", "remaining_units", "burn_units_per_min",
                    "exhaustion_eta_min", "required_reduction_units_per_min",
                    "release_suppressed"):
            assert key in pool_w, f"pool.{window} missing {key!r}"

    acct = snap["accounts"]["A"]
    assert acct["capacity_x"] == pytest.approx(20.0)
    assert acct["5h"]["gate"] == pytest.approx(94.0)
    assert acct["7d"]["gate"] == pytest.approx(80.0)
    assert acct["fable_weekly"]["gate"] == pytest.approx(95.0)
    for window in ("5h", "7d"):
        for key in ("pct", "gate", "remaining_units", "burn_pct_per_min",
                    "pinned_eta_min", "required_reduction_pct_per_min"):
            assert key in acct[window], f"accounts.A.{window} missing {key!r}"
    for key in ("pct", "gate", "level_bound"):
        assert key in acct["fable_weekly"], f"fable_weekly missing {key!r}"

    # a real breach so `binding` is a nested (non-None) dict for this test.
    breach_state = _state({"A": _acct(pct=96.0)})
    breach = _snapshot(state=breach_state)
    assert breach["binding"] is not None
    assert set(breach["binding"].keys()) == {"view", "name", "constraint",
                                              "window", "eta_min"}
    assert breach["binding"]["view"] in ("pool", "account")


# ======================= test_gate_reflects_live_ladder =========================

def test_gate_reflects_live_ladder():
    """Config steps [70,85,90] -> published gate 90, NOT the stale 94
    (finding 6, load-bearing)."""
    cfg = dict(CFG, thresholds={"steps": [70, 85, 90]})
    snap = _snapshot(cfg=cfg)
    assert snap["accounts"]["A"]["5h"]["gate"] == pytest.approx(90.0)
    assert snap["accounts"]["A"]["5h"]["gate"] != pytest.approx(94.0)


# ============================ test_atomic_no_save_state ==========================

def test_atomic_no_save_state(tmp_path, monkeypatch):
    """`save_state` raising must not stop `_pressure_write_json` from
    succeeding — the write goes through `write_json`/`atomic_write_bytes`,
    never `save_state`. A reader sees the old-or-new file, never a partial
    write."""
    target = tmp_path / "pressure.json"
    monkeypatch.setattr(cus, "PRESSURE_JSON", target)

    def _boom(_state):
        raise AssertionError("save_state must never be called by _pressure_write_json")

    monkeypatch.setattr(cus, "save_state", _boom)

    snap1 = {"level": "ok", "marker": "first"}
    cus._pressure_write_json(snap1)
    assert json.loads(target.read_text()) == snap1
    assert not list(tmp_path.glob("*.tmp.*")), "leftover tmp file after write"

    # Overwrite -> the reader must see the fully-written new content, never a
    # partial mix of old/new.
    snap2 = {"level": "elevated", "marker": "second"}
    cus._pressure_write_json(snap2)
    assert json.loads(target.read_text()) == snap2
    assert not list(tmp_path.glob("*.tmp.*")), "leftover tmp file after overwrite"


# ==================== test_pool_and_peraccount_etas_populated ====================

def test_pool_and_peraccount_etas_populated():
    """Inject a synthetic partition with real pinned burn -> both the pool
    and per-account 5h ETAs are populated (not None)."""
    part = FakePartition(pinned={("A", "5h"): 0.02})  # remaining0=1.76 -> eta~88
    snap = _snapshot(partition=part)
    assert snap["pool"]["5h"]["exhaustion_eta_min"] is not None
    assert snap["accounts"]["A"]["5h"]["pinned_eta_min"] is not None
    assert snap["accounts"]["A"]["5h"]["pinned_eta_min"] == pytest.approx(88.0, abs=0.5)
    assert snap["pool"]["5h"]["exhaustion_eta_min"] == pytest.approx(88.0, abs=0.5)


# =================== test_snapshot_pure_over_injected_products ===================

def test_snapshot_pure_over_injected_products(monkeypatch):
    """`_read_active_tails`/`_attribute_burn`/`fit_burn_weights` all RAISE ->
    `_pressure_snapshot` still builds (given the already-injected products)
    -> proves it performs NO Phase-D I/O of its own (round-3 finding 2)."""
    def _boom(*a, **k):
        raise AssertionError("Phase-D I/O must not be reached by _pressure_snapshot")

    monkeypatch.setattr(cus, "_read_active_tails", _boom)
    monkeypatch.setattr(cus, "_attribute_burn", _boom)
    monkeypatch.setattr(cus, "fit_burn_weights", _boom)

    snap = _snapshot()
    assert snap["level"] in ("ok", "elevated", "critical")
    assert snap["weight_fit"]["source"] == "fit"


# ======================== test_sessions_from_injected_table ======================

def test_sessions_from_injected_table():
    """`sessions[]` rows come from the injected `session_table` verbatim
    (shape-normalized), never re-read from `state`."""
    rows = [
        _session_row(session_id="s1", account_shares={"A": 0.02}),
        _session_row(session_id="s2", account_shares={"A": 0.01}),
    ]
    snap = _snapshot(session_table=rows)
    assert len(snap["sessions"]) == 2
    ids = {s["session_id"] for s in snap["sessions"]}
    assert ids == {"s1", "s2"}
    for expected, actual in zip(rows, snap["sessions"]):
        assert actual["account_shares"] == expected["account_shares"]
        assert actual["model"] == expected["model"]
        assert actual["coordinator_of"] == expected["coordinator_of"]


# ======================== test_binding_eta_consistent_240 ========================

def test_binding_eta_consistent_240():
    """A trigger breaching at t=200 -> binding.eta_min == 200 and equals the
    per-account pinned_eta_min it was selected from (round-3 finding 1)."""
    # remaining0 = (94-50)/100*4 = 1.76; pinned = 1.76/200 -> breach at t=200.
    r0 = cus._pressure_remaining_units(50.0, 94.0, RATIO)
    pinned_rate = r0 / 200.0
    # Second pooled account B: huge healthy headroom, zero burn, so the POOL
    # sum never breaches within [0,240] (B's flat contribution keeps the pool
    # total positive) -> only A's per-account pinned trigger is binding, no
    # pool/account tie to disambiguate.
    state = _state({
        "A": _acct(pct=50.0, reset_min=None),
        "B": _acct(pct=0.0, reset_min=None),
    })
    cfg = dict(CFG, accounts=[{"name": "A", "capacity_x": 20},
                              {"name": "B", "capacity_x": 20}])
    part = FakePartition(pinned={("A", "5h"): pinned_rate})
    snap = _snapshot(state=state, cfg=cfg, partition=part)

    assert snap["pool"]["5h"]["exhaustion_eta_min"] is None
    assert snap["accounts"]["A"]["5h"]["pinned_eta_min"] == pytest.approx(200.0, abs=0.5)
    assert snap["binding"] is not None
    assert snap["binding"]["view"] == "account"
    assert snap["binding"]["name"] == "A"
    assert snap["binding"]["eta_min"] == pytest.approx(200.0, abs=0.5)
    assert snap["binding"]["eta_min"] == pytest.approx(
        snap["accounts"]["A"]["5h"]["pinned_eta_min"], abs=1e-9)


# ========================= test_session_table_placeholder ========================

def test_session_table_placeholder():
    """A minimal/placeholder session_table row (most fields absent) is
    normalized to the pinned 11-key shape — missing keys default to None
    (account_shares to {}), never dropped or KeyError'd."""
    snap = _snapshot(session_table=[{"session_id": "only-this"}])
    assert len(snap["sessions"]) == 1
    row = snap["sessions"][0]
    assert row["session_id"] == "only-this"
    assert row["account_shares"] == {}
    for key in ("model", "fable_share", "pane", "socket", "cwd", "class",
               "rate", "trend", "coordinator_of"):
        assert key in row
        assert row[key] is None

    # empty session_table entirely -> sessions: [] (still builds fine).
    empty_snap = _snapshot(session_table=[])
    assert empty_snap["sessions"] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
