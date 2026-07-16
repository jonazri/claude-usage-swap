"""Task 23 (spec-2 token-pressure forecaster, STAGE 1): shadow-mode cycle +
cus-side emit hysteresis + shadow log.

`_pressure_cycle` is the daemon's per-cycle counterpart to `cus pressure`
(Task 21's `pressure_cmd`): the SAME Phase-D I/O (state load is the
CALLER's job here -- `state`/`config` are explicit parameters, not loaded
from disk), except `_read_active_tails(..., persist=True)` -- a real daemon
advances its own transcript-offset registry cycle to cycle. With
`pressure.shadow_mode` true (the default), the cycle atomic-writes
pressure.json and appends a "would-have" record to a per-day shadow log,
then RETURNS -- no §4 emit, no §6 marker/delivery write, ever. With
`shadow_mode: false` it consults the cus-side half of §4's two-place emit
hysteresis (`_pressure_emit_decision`) per currently-binding key and emits
only when admitted.

Also covers the USER-APPROVED safety addition: under cold-start
`attribution["blindness"]`, the real per-message attributed partition is
built from a starved/absent read window and UNDERSTATES burn (an
artificially long, unsafe ETA) -- the cycle instead builds the snapshot's
SUPPLY forecast from cus's own coarse, attribution-independent per-account
rate fields (`_pressure_supply_partition`).

HARNESS (same as every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file; add the repo root to ``sys.path`` and
``import cus``. `_pressure_cycle` takes `state`/`config` as EXPLICIT
parameters (no internal `_pressure_load_state`/`load_config` disk read), so
only the paths its Phase-D I/O and its OWN new pressure-owned artifacts
(pressure.json, the shadow log, the last-emit registry -- all rooted under
`PRESSURE_ROOT`, Task 23's own new constant, the `PRESSURE_JSON` direct-
monkeypatch precedent extended to a whole subtree) need isolating.

Run: ``python3 -m pytest tests/test_pressure_shadow.py -q``.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


# reference_x pinned to 5 (the live production pin, FACT #4); one 20x
# account ("A") gives ratio 4.0 -- the same fleet shape every other
# tests/test_pressure_*.py file uses.
BASE_CFG = {
    "capacity_aware": {"enabled": True, "reference_x": 5},
    "thresholds": {"steps": [70, 85, 94]},   # gate_5h = 94 (live ladder top)
    "per_model_weekly": {"cap_pct": 95},
    "accounts": [{"name": "A", "capacity_x": 20}],
}


def _acct(pct=50.0, pct7d=10.0):
    # `last_poll_ts` set (freshly polled, known `capacity_x`) so the pool
    # view is never spuriously release-suppressed (`_pool_release_suppressed`
    # treats an unpolled/unknown-freshness account as pool-uncertain, which
    # would otherwise make BOTH pool:5h and pool:7d binding regardless of
    # this account's own pct -- not what these fixtures intend to exercise).
    return {"capacity_x": 20, "current_5h_pct": pct, "current_7d_pct": pct7d,
            "last_poll_ts": NOW.isoformat()}


def _set_mtime(path: Path, dt: datetime) -> None:
    ts = dt.timestamp()
    os.utime(path, (ts, ts))


def _env(tmp_path, monkeypatch):
    """Isolated tmp tree for `_pressure_cycle` (Task 23) -- see module
    docstring for why `STATE_JSON`/`CONFIG_YAML` need no monkeypatch here
    (unlike `tests/test_pressure_cli.py`'s `_env`, which drives the
    `cus pressure` CLI command that loads both from disk itself)."""
    monkeypatch.setattr(cus, "CLAUDE_DIR", tmp_path / "claude_home")
    monkeypatch.setattr(cus, "SESSIONS_LOG", tmp_path / "sessions.log")

    accounts_dir = tmp_path / "claude-accounts"
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(cus, "PRESSURE_JSON", accounts_dir / "pressure.json")
    monkeypatch.setattr(cus, "PRESSURE_ROOT", accounts_dir / "pressure")

    # Fresh in-memory offset registry every test (Task 10: caller-owned,
    # in-memory only -- a module-global shared across the whole test
    # session would otherwise leak state between tests).
    monkeypatch.setattr(cus, "_PRESSURE_TAIL_OFFSETS", {})

    return accounts_dir


def _shadow_log_path(accounts_dir: Path, now: datetime) -> Path:
    return accounts_dir / "pressure" / "shadow" / f"{now:%Y-%m-%d}.jsonl"


# =========================== _pressure_cycle (shadow gate) ===========================

def test_shadow_mode_suppresses_emit_and_logs(tmp_path, monkeypatch):
    """A synthetic breaching state (default shadow_mode -- no `pressure`
    config key at all, proving the True default) computes a real snapshot,
    writes pressure.json, and appends exactly one shadow-log line -- but
    NEVER calls the emit/marker spies. `would_emit` is the payload that
    would have fired.

    Follow-up 1, Part 2: `would_ask`/`would_target` are now WIRED to
    `dry_run_target(snapshot, config)` -- no longer the hardcoded
    `None`/`[]` placeholder. Over-gate alone (pct 96 > gate 94) already
    makes `pinned_eta_min` immediate (0.0) regardless of burn rate, so the
    ORIGINAL fixture already had a binding/critical breach -- but with NO
    burn-rate signal at all, `required_reduction_pct_per_min` stayed 0.0
    (nothing to reduce against), which would make `dry_run_target`
    trivially "already met" with an empty `targets` list even after
    wiring, for a reason unrelated to the wiring itself. So this fixture
    ALSO gives account "A" a real coarse burn rate via the same cold-start-
    blindness scaffold `test_blindness_uses_coarse_supply_rate` uses (one
    recently-active, empty-content transcript -> blindness=True ->
    `_pressure_supply_partition` feeds `burn_rate_5h_pct_per_min` into the
    published pinned burn), so `required_reduction_pct_per_min` is
    genuinely non-zero. One real elastic candidate session is injected via
    a monkeypatched `_pressure_build_session_table` (the transcript-
    attribution machinery that would normally produce a session_table
    organically is Task 11-13's concern, out of scope for this wiring test
    -- same "hand-built to the exact schema" approach
    `tests/test_pressure_dryrun.py` documents for `dry_run_target` itself),
    so the resulting plan is a real, non-empty targeting plan.
    """
    accounts_dir = _env(tmp_path, monkeypatch)
    state = {"accounts": {"A": _acct(pct=96.0, pct7d=10.0)}}  # 96 > gate 94
    state["accounts"]["A"]["burn_rate_5h_pct_per_min"] = 5.0
    state["accounts"]["A"]["burn_rate_7d_pct_per_min"] = 0.5

    # Cold-start blindness scaffold (same technique as
    # `test_blindness_uses_coarse_supply_rate` below): one recently-active,
    # empty-content transcript is enough for `_read_active_tails` to see an
    # ACTIVE session against the empty pre-cycle offsets registry, which is
    # what drives the coarse burn-rate fallback above.
    slug_dir = cus.CLAUDE_DIR / "projects" / "proj"
    slug_dir.mkdir(parents=True)
    transcript = slug_dir / "sess1.jsonl"
    transcript.write_text("")
    _set_mtime(transcript, NOW)

    # share%/min = rate(40.0) * account_shares["A"](1.0) = 40.0, well above
    # share_floor_pct's default 15.0 -- a real §5.2 candidate for the
    # account:A:5h breach even though trend is "steady" (not "rising").
    wf_row = {
        "session_id": "wf-shadow-1", "account_shares": {"A": 1.0}, "model": None,
        "fable_share": None, "pane": "%1", "socket": "s0", "cwd": "/x",
        "class": "workflow", "rate": 40.0, "trend": "steady", "coordinator_of": None,
    }
    monkeypatch.setattr(cus, "_pressure_build_session_table", lambda *a, **k: [wf_row])

    emit_calls = []
    marker_calls = []
    monkeypatch.setattr(cus, "_pressure_emit_socket", lambda payload: emit_calls.append(payload))
    monkeypatch.setattr(cus, "_pressure_write_emit_marker",
                        lambda key, payload, now: marker_calls.append((key, payload)))

    snapshot = cus._pressure_cycle(state, BASE_CFG, NOW)

    assert emit_calls == [], "shadow_mode must never reach the emit socket"
    assert marker_calls == [], "shadow_mode must never reach the §6 marker write"
    assert snapshot["level"] != "ok"
    assert cus.PRESSURE_JSON.exists()

    log_path = _shadow_log_path(accounts_dir, NOW)
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["level"] == snapshot["level"]
    assert record["binding"] == snapshot["binding"]
    assert record["would_emit"] is not None
    assert record["would_emit"]["severity"] == snapshot["level"]
    assert record["would_emit"]["episode_id"] is None

    expected_plan = cus.dry_run_target(snapshot, BASE_CFG)
    assert record["would_ask"] == expected_plan
    assert record["would_target"] == expected_plan["targets"]
    assert record["would_target"] != [], (
        "a real elastic candidate must produce a non-empty would_target plan"
    )
    assert record["would_target"][0]["session_id"] == "wf-shadow-1"

    assert record["reset_models"]["rolling_integral"] is None
    assert "decayed_step" in record["reset_models"]
    assert record["pool"] == snapshot["pool"]
    assert record["per_account"] == snapshot["accounts"]
    assert record["weight_fit"] == snapshot["weight_fit"]


def test_would_ask_and_would_target_stay_none_when_no_breach():
    """Follow-up 1, Part 2: when there is no binding (`level == "ok"`),
    `would_ask`/`would_target` must stay `None`/`[]` -- `dry_run_target` is
    not even consulted for a no-breach snapshot (its OWN `binding is None`
    branch returns a "trivially met" dict, which is a different thing from
    "there was no ask to log")."""
    state = {"accounts": {}}
    snapshot = {
        "level": "ok", "reference_x": 5.0, "safety_factor": 1.0,
        "binding": None,
        "pool": {
            "5h": {"exhaustion_eta_min": None, "required_reduction_units_per_min": 0.0,
                   "release_suppressed": False},
            "7d": {"exhaustion_eta_min": None, "required_reduction_units_per_min": 0.0,
                   "release_suppressed": False},
        },
        "accounts": {}, "sessions": [], "weight_fit": {"weights": {}},
    }
    record = cus._pressure_build_shadow_record(state, snapshot, {"accounts": []}, NOW)
    assert record["would_ask"] is None
    assert record["would_target"] == []


def test_shadow_false_still_emits(tmp_path, monkeypatch):
    """`shadow_mode: false` -> the emit spy fires once for the one binding
    key, and NO shadow-log line is written this cycle."""
    accounts_dir = _env(tmp_path, monkeypatch)
    state = {"accounts": {"A": _acct(pct=96.0, pct7d=10.0)}}
    config = dict(BASE_CFG, pressure={"shadow_mode": False})

    emit_calls = []
    monkeypatch.setattr(cus, "_pressure_emit_socket", lambda payload: emit_calls.append(payload))
    monkeypatch.setattr(cus, "_pressure_write_emit_marker", lambda key, payload, now: None)

    snapshot = cus._pressure_cycle(state, config, NOW)

    assert snapshot["level"] != "ok"
    assert len(emit_calls) == 1
    assert emit_calls[0]["severity"] == snapshot["level"]

    log_path = _shadow_log_path(accounts_dir, NOW)
    assert not log_path.exists()


def test_shadow_would_emit_respects_hysteresis(tmp_path, monkeypatch):
    """Fix wave 1 (Important finding): shadow's `would_emit` must reflect
    the SAME cross-cycle hysteresis the live path would apply, via its OWN
    `last_would_emit.json` registry -- never the live path's `last_emit.json`
    (which stays permanently empty in shadow-only operation, the Stage-1
    default, since only the `shadow_mode: false` branch ever writes it).
    Pre-fix, `would_emit` fired on EVERY cycle regardless of level/cooldown/
    growth because `_pressure_emit_decision` always consulted that
    permanently-empty live registry and so always saw a first-ever-emit
    prior.

    Five cycles, 5 minutes apart, all well within the default 20-min
    `reemit_cooldown_min`, against a PERSISTENT `PRESSURE_ROOT` (one `_env`
    call, `_pressure_cycle` invoked five times -- the registry is real
    on-disk state carried cycle to cycle, exactly as a real daemon would
    see it). The account's `pct` never changes (a sustained same-level,
    same-`required_reduction` condition -- no growth) -- only
    `pressure.critical_eta_min` is varied to move the key's classification
    from "elevated" to "critical" for cycles 3-4, an upward transition that
    must bypass the cooldown even though it hasn't elapsed.
    """
    accounts_dir = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(cus, "_pressure_emit_socket", lambda payload: None)
    monkeypatch.setattr(cus, "_pressure_write_emit_marker", lambda key, payload, now: None)

    state = {"accounts": {"A": _acct(pct=96.0, pct7d=10.0)}}  # 96 > gate 94, sustained
    # cycles 0-2: critical_eta_min so low (-1) that pinned_eta_min=0.0 does
    # NOT qualify as critical (0.0 < -1 is False) -- the key reads
    # "elevated". cycles 3-4: default critical_eta_min=60 -- 0.0 < 60 -- the
    # SAME underlying condition now reads "critical": an upward transition.
    critical_eta_mins = [-1, -1, -1, 60, 60]
    would_emit_fired = []

    for i, critical_eta_min in enumerate(critical_eta_mins):
        now = NOW + timedelta(minutes=5 * i)
        config = dict(BASE_CFG, pressure={"critical_eta_min": critical_eta_min})
        cus._pressure_cycle(state, config, now)

        log_path = _shadow_log_path(accounts_dir, now)
        record = json.loads(log_path.read_text().splitlines()[-1])
        would_emit_fired.append(record["would_emit"] is not None)

    assert would_emit_fired == [True, False, False, True, False], (
        "expected: fires cycle 0 (first-ever), suppressed cycles 1-2 (same "
        "level, within cooldown, no growth), fires cycle 3 (upward "
        "transition elevated->critical, bypasses cooldown), suppressed "
        f"cycle 4 (same level again, within cooldown) -- got {would_emit_fired}"
    )

    registry_path = accounts_dir / "pressure" / "last_would_emit.json"
    assert registry_path.exists(), "shadow must persist its OWN would-emit registry"
    registry = json.loads(registry_path.read_text())
    key = "token-pressure:account:A:5h"
    assert registry[key]["level"] == "critical"
    assert registry[key]["ts"] == (NOW + timedelta(minutes=15)).isoformat()

    # The live path's OWN registry must never be touched by shadow cycles.
    assert not (accounts_dir / "pressure" / "last_emit.json").exists()


# =========================== _pressure_emit_decision ===========================

def _pool_snapshot(eta_5h=None, rr_5h=0.0):
    return {
        "pool": {
            "5h": {"exhaustion_eta_min": eta_5h, "required_reduction_units_per_min": rr_5h,
                   "release_suppressed": False},
            "7d": {"exhaustion_eta_min": None, "required_reduction_units_per_min": 0.0,
                   "release_suppressed": False},
        },
        "accounts": {},
    }


def _account_snapshot(eta_5h=None, rr_5h=0.0):
    return {
        "pool": {
            "5h": {"exhaustion_eta_min": None, "required_reduction_units_per_min": 0.0,
                   "release_suppressed": False},
            "7d": {"exhaustion_eta_min": None, "required_reduction_units_per_min": 0.0,
                   "release_suppressed": False},
        },
        "accounts": {
            "A": {"5h": {"pinned_eta_min": eta_5h, "required_reduction_pct_per_min": rr_5h}},
        },
    }


def test_emit_on_upward_transition():
    """elevated -> critical for an ALREADY-tracked key admits even well
    within the 20-min cooldown (bypass)."""
    key = "token-pressure:pool:5h"
    last_emit = {"level": "elevated", "required_reduction": 0.05,
                "ts": (NOW - timedelta(minutes=5)).isoformat()}
    snapshot = _pool_snapshot(eta_5h=30.0, rr_5h=0.06)  # 30 < critical_eta_min (60) -> critical

    admit, reason = cus._pressure_emit_decision(key, snapshot, last_emit, BASE_CFG, NOW)

    assert admit is True
    assert "upward" in reason


def test_emit_on_newly_critical_key():
    """A key with no prior track record that comes up already critical
    admits (an implicit ok->critical upward transition, and simultaneously
    "newly critical" -- both of §4's first two triggers)."""
    key = "token-pressure:account:A:5h"
    snapshot = _account_snapshot(eta_5h=15.0, rr_5h=5.0)  # 15 < 60 -> critical

    admit, reason = cus._pressure_emit_decision(key, snapshot, None, BASE_CFG, NOW)

    assert admit is True
    assert "critical" in reason


def test_emit_on_required_reduction_growth_over_25pct():
    """Sustained elevated (SAME level both before and after), ask jumps
    8 -> 20 (%/min magnitude, 150% growth) well within the 20-min cooldown
    -> re-emits anyway (§4's third trigger, the growth bypass)."""
    key = "token-pressure:account:A:5h"
    last_emit = {"level": "elevated", "required_reduction": 8.0,
                "ts": (NOW - timedelta(minutes=5)).isoformat()}
    snapshot = _account_snapshot(eta_5h=100.0, rr_5h=20.0)  # still elevated (>=60)

    admit, reason = cus._pressure_emit_decision(key, snapshot, last_emit, BASE_CFG, NOW)

    assert admit is True
    assert "grew" in reason or "growth" in reason


def test_no_reemit_same_level_within_cooldown():
    """Same level, required_reduction growth well under 25%, within the
    20-min cooldown -> suppressed."""
    key = "token-pressure:account:A:5h"
    last_emit = {"level": "elevated", "required_reduction": 8.0,
                "ts": (NOW - timedelta(minutes=5)).isoformat()}
    snapshot = _account_snapshot(eta_5h=100.0, rr_5h=8.5)  # growth 6.25% <= 25%

    admit, reason = cus._pressure_emit_decision(key, snapshot, last_emit, BASE_CFG, NOW)

    assert admit is False
    assert "cooldown" in reason


def test_no_reemit_ok_level():
    """A key that is not currently binding at all (level "ok") never
    admits, regardless of `last_emit`."""
    key = "token-pressure:pool:5h"
    snapshot = _pool_snapshot(eta_5h=None, rr_5h=0.0)  # no ETA -> "ok"

    admit, reason = cus._pressure_emit_decision(key, snapshot, None, BASE_CFG, NOW)

    assert admit is False
    assert "ok" in reason


def test_reemit_after_cooldown_elapses():
    """Same level, growth under 25%, but the cooldown window has fully
    elapsed -> admits (the ordinary, non-bypass re-emit path)."""
    key = "token-pressure:account:A:5h"
    last_emit = {"level": "elevated", "required_reduction": 8.0,
                "ts": (NOW - timedelta(minutes=25)).isoformat()}  # > 20min default
    snapshot = _account_snapshot(eta_5h=100.0, rr_5h=8.2)

    admit, reason = cus._pressure_emit_decision(key, snapshot, last_emit, BASE_CFG, NOW)

    assert admit is True
    assert "cooldown elapsed" in reason


# =========================== blindness -> coarse supply rate ===========================

def test_blindness_uses_coarse_supply_rate(tmp_path, monkeypatch):
    """Under cold-start blindness (one real, recently-active transcript,
    empty in-memory offsets -> nothing yet read -> blindness=True per Task
    19's own rule), the snapshot's per-account burn is built from cus's
    OWN coarse `burn_rate_5h_pct_per_min` (attribution-independent) rather
    than the understated (here: literally zero, empty transcript) attributed
    partition. A 20x account (ratio 4.0) with a coarse rate of 2.0%/min
    round-trips through `_pressure_burn_units`/`burn_pct_per_min` back to
    2.0 -- the real attributed partition would instead read ~0.0 (nothing
    was attributed from the empty transcript), so this is a precise,
    unambiguous proof of which partition drove the published number."""
    accounts_dir = _env(tmp_path, monkeypatch)

    # One recently-active (mtime == NOW), zero-content transcript: enough
    # for `_read_active_tails`/`_pressure_transcript_paths` to see an
    # ACTIVE session (triggering blindness against the empty pre-cycle
    # offsets registry), while contributing ~nothing to attribution.
    slug_dir = cus.CLAUDE_DIR / "projects" / "proj"
    slug_dir.mkdir(parents=True)
    transcript = slug_dir / "sess1.jsonl"
    transcript.write_text("")
    _set_mtime(transcript, NOW)

    state = {"accounts": {"A": _acct(pct=50.0, pct7d=10.0)}}
    state["accounts"]["A"]["burn_rate_5h_pct_per_min"] = 2.0
    state["accounts"]["A"]["burn_rate_7d_pct_per_min"] = 0.5

    monkeypatch.setattr(cus, "_pressure_emit_socket", lambda payload: None)
    monkeypatch.setattr(cus, "_pressure_write_emit_marker", lambda key, payload, now: None)

    snapshot = cus._pressure_cycle(state, BASE_CFG, NOW)

    assert snapshot["attribution"]["blindness"] is True
    assert snapshot["accounts"]["A"]["5h"]["burn_pct_per_min"] == pytest.approx(2.0, abs=1e-6)
    assert accounts_dir.joinpath("pressure.json").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
