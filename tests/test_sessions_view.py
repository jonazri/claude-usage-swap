"""Tests for `cus sessions` pure logic (GH #137 diagnostics view).

Covers the daemon-less, I/O-free helpers behind the command so the drift +
binding-constraint logic is exercised without tmux/proc:
  - _session_binding: 5h/7d hard cap, premium per-model weekly gate, standard-
    pool gate bypass, ladder-step warn, headroom, token/rate blockers.
  - build_session_rows: DRIFT flag (sessions.log label vs resolved mount) and
    bare/unresolved handling.
  - detect_slot_orphans: slots with live pids and no owning pane.

Run standalone:  python3 tests/test_sessions_view.py
Run under pytest: pytest tests/test_sessions_view.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _config(**overrides) -> dict:
    """per_session config with the Fable weekly gate ON at cap 97%, mirroring
    the live setup that surfaced #137 (premium slots evacuate a Fable-capped
    account; standard slots keep it)."""
    cfg = cus.deep_merge(cus.DEFAULT_CONFIG, {
        "mode": "per_session",
        "thresholds": {"steps": [50, 75, 90]},
        "per_model_weekly": {"gate_enabled": True, "cap_pct": 97},
    })
    return cus.deep_merge(cfg, overrides)


def _acct(five=0.0, seven=0.0, per_model=None, **flags) -> dict:
    a = {"current_5h_pct": five, "current_7d_pct": seven}
    if per_model is not None:
        a["per_model_weekly_pct"] = per_model
    a.update(flags)
    return a


# --------------------------------------------------------------------------
# _session_binding
# --------------------------------------------------------------------------

def test_binding_5h_hard_cap():
    sev, txt = cus._session_binding(_acct(five=100.0), "premium", _config())
    assert sev == "blocked"
    assert "5h" in txt and "hard cap" in txt


def test_binding_premium_model_gate_blocks():
    sev, txt = cus._session_binding(_acct(five=10.0, per_model={"Fable": 98.0}), "premium", _config())
    assert sev == "blocked"
    assert "Fable" in txt and "premium gate" in txt


def test_binding_standard_pool_ignores_model_gate():
    # Same Fable=98% account, but on a STANDARD lane: not blocked, and the
    # number is surfaced-but-ignored rather than hidden (two-dimensional
    # exhaustion — standard-model work doesn't touch the exhausted model).
    sev, txt = cus._session_binding(_acct(five=10.0, per_model={"Fable": 98.0}), "standard", _config())
    assert sev == "ok"
    assert "ignored" in txt and "Fable" in txt


def test_binding_ladder_step_warn():
    sev, txt = cus._session_binding(_acct(five=78.0), "premium", _config())
    assert sev == "warn"
    assert "ladder step 75%" in txt


def test_binding_headroom_ok():
    sev, txt = cus._session_binding(_acct(five=22.0, seven=3.0, per_model={"Fable": 3.0}), "premium", _config())
    assert sev == "ok"
    assert "headroom" in txt


def test_binding_token_expired_beats_percentages():
    # A cached 0% must NOT read as healthy when the token is dead.
    sev, txt = cus._session_binding(_acct(five=0.0, token_expired=True), "premium", _config())
    assert sev == "blocked"
    assert "token expired" in txt


def test_binding_gate_off_when_disabled():
    # Same Fable=98% but gate disabled in config → the model number does not
    # block; only the aggregate ladder/5h can.
    cfg = _config(per_model_weekly={"gate_enabled": False, "cap_pct": 97})
    sev, _txt = cus._session_binding(_acct(five=10.0, per_model={"Fable": 98.0}), "premium", cfg)
    assert sev == "ok"


# --------------------------------------------------------------------------
# build_session_rows — DRIFT detection (#137)
# --------------------------------------------------------------------------

def _state() -> dict:
    return {
        "active": "merkos",
        "accounts": {
            "rayi2": _acct(five=5.0, seven=10.0),
            "merkos": _acct(five=50.0, seven=20.0),
        },
        "slots": {"slot-3": {"account": "rayi2", "pool": "premium"}},
    }


def test_row_flags_drift_when_log_disagrees_with_mount():
    # sessions.log said 'merkos' (launch-time label) but the live mount resolves
    # to rayi2 — the exact #137 signature.
    rows = cus.build_session_rows([{
        "session_id": "abc123", "pane": "%4", "cwd": "/x",
        "logged_account": "merkos", "mount": "slot-3",
        "resolved_account": "rayi2", "source": "disk-identity",
    }], _state(), _config())
    assert len(rows) == 1
    r = rows[0]
    assert r["drift"] is True
    assert r["account"] == "rayi2"
    assert r["logged_account"] == "merkos"
    assert r["slot"] == "slot-3"
    assert r["pool"] == "premium"


def test_row_no_drift_when_aligned():
    rows = cus.build_session_rows([{
        "session_id": "abc123", "pane": "%4", "cwd": "/x",
        "logged_account": "rayi2", "mount": "slot-3",
        "resolved_account": "rayi2", "source": "state-slot",
    }], _state(), _config())
    assert rows[0]["drift"] is False


def test_row_bare_session_never_drifts():
    # A bare/global pane has no mount to compare — "follows global creds" is by
    # design, NOT the #137 defect, so it must not be flagged.
    rows = cus.build_session_rows([{
        "session_id": "abc123", "pane": "%9", "cwd": "/x",
        "logged_account": "merkos", "mount": None,
        "resolved_account": None, "source": "unresolved",
    }], _state(), _config())
    assert rows[0]["drift"] is False
    assert rows[0]["bare"] is True


# --------------------------------------------------------------------------
# build_session_rows — drift_kind framing (2026-07-12 sessions-view incident)
# --------------------------------------------------------------------------

def test_row_drift_from_state_slot_is_lane_moved():
    # Resolution came from the daemon's own record (state-slot) and disagrees
    # with the launch label -> the lane was swapped AFTER launch. Benign,
    # informational: drift is True but drift_kind is "lane-moved".
    rows = cus.build_session_rows([{
        "session_id": "abc123", "pane": "%0", "cwd": "/x",
        "logged_account": "yaz-myjli-com-max", "mount": "slot-3",
        "resolved_account": "rayi2", "source": "state-slot",
    }], _state(), _config())
    assert rows[0]["drift"] is True
    assert rows[0]["drift_kind"] == "lane-moved"


def test_row_drift_from_disk_identity_is_clobber():
    # The on-disk identity had to OVERRIDE the record (disk-identity on a real
    # mount) -> a foreign account is mounted in place. This is the alarm case.
    rows = cus.build_session_rows([{
        "session_id": "abc123", "pane": "%0", "cwd": "/x",
        "logged_account": "merkos", "mount": "slot-3",
        "resolved_account": "rayi2", "source": "disk-identity",
    }], _state(), _config())
    assert rows[0]["drift"] is True
    assert rows[0]["drift_kind"] == "identity-clobber"


def test_row_no_drift_has_no_drift_kind():
    rows = cus.build_session_rows([{
        "session_id": "abc123", "pane": "%4", "cwd": "/x",
        "logged_account": "rayi2", "mount": "slot-3",
        "resolved_account": "rayi2", "source": "state-slot",
    }], _state(), _config())
    assert rows[0]["drift"] is False
    assert rows[0]["drift_kind"] is None


# --------------------------------------------------------------------------
# _resolve_mount_account — sibling/`default` identity-collapse (2026-07-12)
# --------------------------------------------------------------------------
#
# THE INCIDENT: `cus sessions` printed one account (the global active) with one
# usage row for EVERY live slotted session. Root cause: the resolution ladder
# tried on-disk oauthAccount FIRST for slot mounts, and
# _account_for_mount_identity returns the FIRST account in state["accounts"]
# whose identity matches. Because pro/max sibling PAIRS and the shared `default`
# account all carry ONE identical oauthAccount identity, first-match collapsed
# distinct slots onto whichever sharing account sorted first — never the actual
# occupant the daemon recorded in state.slots. The fix makes state.slots the
# primary evidence for slot mounts (it alone disambiguates siblings) and keeps
# on-disk identity as a cross-check that fires only for a FOREIGN family.

# Two families, each a pro/max pair sharing one uuid+email, plus a `default`
# account that (as on the live box) also shares the tefillin identity and sorts
# FIRST in the accounts dict — the exact shadowing that produced the bug.
_UUID_MYJLI = "5e45e2f7-4302-420b-bb25-3f5492d56304"
_UUID_TEF = "e2a6eec3-ada2-4074-87f3-76f9c62e28ab"
_FAMILY = {
    "default": (_UUID_TEF, "yaz@tefillinconnection.org"),
    "yaz-myjli-com": (_UUID_MYJLI, "yaz@myjli.com"),
    "yaz-myjli-com-max": (_UUID_MYJLI, "yaz@myjli.com"),
    "yaz-tefillinconnection-org": (_UUID_TEF, "yaz@tefillinconnection.org"),
    "yaz-tefillinconnection-org-max": (_UUID_TEF, "yaz@tefillinconnection.org"),
}


def _write_cj(d: Path, uuid: str, email: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / ".claude.json").write_text(json.dumps(
        {"userID": f"uid-{email}", "oauthAccount": {"accountUuid": uuid, "emailAddress": email}}))


def _resolve_env(tmp_path, monkeypatch, slots: dict[str, tuple[str, str]]):
    """Repoint ACCOUNTS_DIR at a tmp tree with the shared-identity account
    snapshots and the given slot mounts. `slots` maps slot-name -> the
    (uuid, email) identity written into that slot's live .claude.json.
    Returns the state dict (accounts insertion order preserved => `default`
    first, siblings adjacent, mirroring the live box)."""
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", tmp_path)
    for name, (uuid, email) in _FAMILY.items():
        _write_cj(tmp_path / f"account-{name}", uuid, email)
    for slot, (uuid, email) in slots.items():
        _write_cj(tmp_path / slot, uuid, email)
    return {"accounts": {n: {} for n in _FAMILY}}


def test_slot_resolves_to_recorded_max_sibling_not_first_match(tmp_path, monkeypatch):
    # slot-2 holds the MAX myjli sibling; its on-disk identity is shared with the
    # non-max sibling, which sorts first. Pre-fix: first-match returned
    # 'yaz-myjli-com'. Post-fix: the recorded occupant wins.
    state = _resolve_env(tmp_path, monkeypatch, {"slot-2": _FAMILY["yaz-myjli-com-max"]})
    state["slots"] = {"slot-2": {"account": "yaz-myjli-com-max", "pool": "premium"}}
    assert cus._resolve_mount_account("slot-2", state) == ("yaz-myjli-com-max", "state-slot")


def test_slot_not_shadowed_by_default_account(tmp_path, monkeypatch):
    # slot-3/slot-4 carry the tefillin identity, which `default` (sorts first)
    # ALSO carries. Pre-fix: both resolved to 'default'. Post-fix: each resolves
    # to its own recorded tefillin occupant (max vs non-max disambiguated).
    state = _resolve_env(tmp_path, monkeypatch, {
        "slot-3": _FAMILY["yaz-tefillinconnection-org-max"],
        "slot-4": _FAMILY["yaz-tefillinconnection-org"],
    })
    state["slots"] = {
        "slot-3": {"account": "yaz-tefillinconnection-org-max", "pool": "premium"},
        "slot-4": {"account": "yaz-tefillinconnection-org", "pool": "premium"},
    }
    assert cus._resolve_mount_account("slot-3", state) == ("yaz-tefillinconnection-org-max", "state-slot")
    assert cus._resolve_mount_account("slot-4", state) == ("yaz-tefillinconnection-org", "state-slot")


def test_slot_disk_identity_overrides_foreign_family(tmp_path, monkeypatch):
    # A genuine in-place clobber: the daemon recorded a tefillin occupant but the
    # slot's live .claude.json now carries the MYJLI identity. The cross-check
    # must override the stale record with the on-disk (foreign) family.
    state = _resolve_env(tmp_path, monkeypatch, {"slot-4": _FAMILY["yaz-myjli-com"]})
    state["slots"] = {"slot-4": {"account": "yaz-tefillinconnection-org", "pool": "premium"}}
    acct, source = cus._resolve_mount_account("slot-4", state)
    assert source == "disk-identity"
    assert acct in ("yaz-myjli-com", "yaz-myjli-com-max")  # a myjli-family name


def test_account_dir_mount_keeps_dir_name_over_sibling(tmp_path, monkeypatch):
    # An account-<name> mount for the MAX sibling: the shared on-disk identity
    # also matches the non-max sibling (sorts first). The dir name must win —
    # identity is only a cross-check for a foreign family.
    state = _resolve_env(tmp_path, monkeypatch, {"account-yaz-myjli-com-max": _FAMILY["yaz-myjli-com-max"]})
    state["slots"] = {}
    assert cus._resolve_mount_account("account-yaz-myjli-com-max", state) == (
        "yaz-myjli-com-max", "account-dir")


def test_unrecorded_slot_falls_back_to_disk_identity(tmp_path, monkeypatch):
    # A slot with NO state.slots record (daemon hasn't claimed it): on-disk
    # identity is the only evidence, so it resolves via disk-identity.
    state = _resolve_env(tmp_path, monkeypatch, {"slot-9": _FAMILY["yaz-myjli-com"]})
    state["slots"] = {}
    acct, source = cus._resolve_mount_account("slot-9", state)
    assert source == "disk-identity"
    assert acct in ("yaz-myjli-com", "yaz-myjli-com-max")


def test_bare_mount_is_unresolved(tmp_path, monkeypatch):
    state = _resolve_env(tmp_path, monkeypatch, {})
    state["slots"] = {}
    assert cus._resolve_mount_account(None, state) == (None, "unresolved")


# --------------------------------------------------------------------------
# detect_slot_orphans
# --------------------------------------------------------------------------

def test_orphan_detected_when_pids_but_no_pane():
    orphans = cus.detect_slot_orphans({"slot-3": 2, "slot-5": 1}, {"slot-5"})
    assert orphans == [{"slot": "slot-3", "pids": 2}]


def test_no_orphans_when_every_slot_has_a_pane():
    orphans = cus.detect_slot_orphans({"slot-3": 1}, {"slot-3"})
    assert orphans == []


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
