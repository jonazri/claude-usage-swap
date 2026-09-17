"""Reactive-429 path fixes from the upstream PR #198 dual review (2026-09-04)
and the Copilot review of PR #187, applied to the fork on 2026-09-17.

  F-F-1  a reactive escape takes a merely DEGRADED target; only a WALLED one
         (>= reactive.max_target_pct) is refused
  F-F-2  held events expire after reactive.pending_ttl_seconds; a held/moved
         slot is withheld from the ladder individually (skip_slots), not the
         whole cycle
  F-F-3  a slot-bound event whose session is gone is settled, not acted on
  F-F-4  the legacy-record stale-generation guard is keyed on the SLOT's own
         install time, not the account-global last_swap_ts
  F-F-7  the injected resume text always carries the automated-agent tag
  F-F-8  a comma in the hook's source/tool field cannot shift slot/account
  F-F-9  resume_after_slot_swap=false sends nothing
  Copilot: any exception during a lane move re-queues the 429 event

Run standalone:  python3 tests/test_reactive_review_fixes_20260917.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cus  # noqa: E402
from test_per_session_cycle import _Env, _config, _usage  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _ago(seconds: float) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def _slot_event(slot: str, account: str, sid: str = "sA", ts: str | None = None) -> dict:
    return {"ts": ts or cus.now_iso(), "session_id": sid, "match": "rate_limit",
            "source": "stopfailure", "slot": slot, "account": account}


# --------------------------------------------------------------------------
# F-F-1: degraded-but-unwalled targets are taken; walled ones are refused
# --------------------------------------------------------------------------

def test_per_session_reactive_takes_degraded_target_with_headroom():
    env = _Env()
    try:
        slot = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 100.0, "current_7d_pct": 10.0})
        # Every candidate is over the 7d hard cap (80) → the picker is DEGRADED,
        # but each has real 5h headroom.
        for name, pct in (("beta", 20.0), ("gamma", 30.0), ("delta", 35.0)):
            state["accounts"][name].update({"current_5h_pct": pct, "current_7d_pct": 85.0})
        original = cus.session_current_slot
        cus.session_current_slot = lambda sid: slot if sid == "sA" else None
        try:
            moves = cus.check_rate_limit_reactive_per_session(
                state, _config(), entries=[_slot_event(slot, "alpha")])
        finally:
            cus.session_current_slot = original
        assert moves and moves[0]["slot"] == slot, moves
        assert moves[0]["to"] == "beta"
        assert "[DEGRADED:" in moves[0]["reason"], "the escape is taken even though the picker degraded"
    finally:
        env.restore()


def test_per_session_reactive_refuses_walled_target():
    env = _Env()
    try:
        slot = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 100.0, "current_7d_pct": 10.0})
        for name in ("beta", "gamma", "delta"):
            state["accounts"][name].update({"current_5h_pct": 96.0, "current_7d_pct": 10.0})
        original = cus.session_current_slot
        cus.session_current_slot = lambda sid: slot if sid == "sA" else None
        event = _slot_event(slot, "alpha")
        try:
            assert cus.check_rate_limit_reactive_per_session(state, _config(), entries=[event]) == []
        finally:
            cus.session_current_slot = original
        assert event.get("_retry") is True
        assert state["_reactive_retry_slots"] == [slot]
    finally:
        env.restore()


def test_max_target_pct_is_configurable():
    acct = {"current_5h_pct": 90.0, "current_7d_pct": 10.0}
    target = cus.SwapTarget(name="x", reason="r [DEGRADED: all candidates would re-trip own ladder]")
    assert target.degraded is True
    assert cus._reactive_target_unsafe(target, acct, _config()) is None
    strict = _config(reactive={"max_target_pct": 85})
    assert "max_target_pct" in (cus._reactive_target_unsafe(target, acct, strict) or "")
    assert cus._reactive_target_unsafe(target, {"token_expired": True}, _config()) == "target token_expired"


# --------------------------------------------------------------------------
# F-F-2: TTL on held events + per-slot ladder preemption
# --------------------------------------------------------------------------

def test_pending_429_entries_expire_after_ttl():
    env = _Env()
    try:
        state = cus.load_state()
        stale = {"ts": _ago(2 * 3600), "session_id": "old00001", "match": "rate_limit",
                 "source": "stopfailure", "slot": "slot-9", "account": "alpha"}
        fresh = {"ts": _ago(60), "session_id": "new00001", "match": "rate_limit",
                 "source": "stopfailure", "slot": "slot-8", "account": "beta"}
        state["pending_429_entries"] = [stale, fresh]
        claimed = cus._claim_rate_limit_entries(state, _config())
        assert [e["session_id"] for e in claimed] == ["new00001"]
        state["pending_429_entries"] = [stale]
        assert cus._claim_rate_limit_entries(state, _config(reactive={"pending_ttl_seconds": 0})) == [stale], \
            "ttl 0 disables expiry"
    finally:
        env.restore()


def test_decide_slot_swaps_skips_only_the_reactive_owned_slot():
    env = _Env()
    try:
        slot_a = env.make_slot("alpha", live=True)
        slot_b = env.make_slot("beta", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 85.0, "current_7d_pct": 10.0})
        state["accounts"]["beta"].update({"current_5h_pct": 85.0, "current_7d_pct": 10.0})
        cfg = _config()
        usage = {"alpha": _usage(85.0, 10.0), "beta": _usage(85.0, 10.0),
                 "gamma": _usage(0.0, 0.0), "delta": _usage(0.0, 0.0)}
        moves = cus.decide_slot_swaps(state, cfg, usage, {}, exclude_accounts={"alpha", "beta"},
                                      skip_slots={slot_a})
        assert [m["slot"] for m in moves] == [slot_b], moves
    finally:
        env.restore()


# --------------------------------------------------------------------------
# F-F-3 / F-F-4: dead-session events settle; slot-level stale-generation guard
# --------------------------------------------------------------------------

def test_slot_bound_event_with_no_live_session_is_settled():
    env = _Env()
    try:
        slot = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 100.0})
        orig_slot, orig_live = cus.session_current_slot, cus.live_sessions_on_slot
        cus.session_current_slot = lambda sid: None
        cus.live_sessions_on_slot = lambda s: []
        event = _slot_event(slot, "alpha")
        try:
            assert cus.check_rate_limit_reactive_per_session(state, _config(), entries=[event]) == []
        finally:
            cus.session_current_slot, cus.live_sessions_on_slot = orig_slot, orig_live
        assert not event.get("_retry"), "a dead session's 429 is settled, not held"
    finally:
        env.restore()


def test_legacy_record_stale_guard_uses_the_slot_install_time():
    env = _Env()
    try:
        slot = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 100.0, "current_7d_pct": 10.0,
                                           "last_swap_ts": cus.now_iso()})  # installed elsewhere just now
        state["slots"][slot]["last_swap_ts"] = _ago(2 * 86400)  # this lane has held alpha for days
        original = cus.session_current_slot
        cus.session_current_slot = lambda sid: slot if sid == "sA" else None
        legacy = {"ts": _ago(3600), "session_id": "sA", "match": "rate_limit", "source": "stopfailure"}
        try:
            moves = cus.check_rate_limit_reactive_per_session(state, _config(), entries=[legacy])
        finally:
            cus.session_current_slot = original
        assert moves and moves[0]["slot"] == slot, "a genuine legacy 429 must not be settled as stale"
    finally:
        env.restore()


# --------------------------------------------------------------------------
# F-F-7 / F-F-9: injected text is tagged; resume off sends nothing
# --------------------------------------------------------------------------

class _S:
    def __init__(self, pane: str, sock: str) -> None:
        self.pane, self.tmux_socket = pane, sock


def _capture_resume(config: dict) -> tuple[list[str], list[tuple]]:
    saved = (cus.live_sessions_on_slot, cus.tmux_send_keys, cus.tmux_send_text, cus.time.sleep)
    keys: list[tuple] = []
    texts: list[tuple] = []
    cus.live_sessions_on_slot = lambda slot: [_S("%1", "/tmp/tmux-a")]
    cus.tmux_send_keys = lambda pane, *sent, tmux_socket=None: keys.append((pane, *sent)) or True
    cus.tmux_send_text = lambda pane, message, tmux_socket=None: texts.append((pane, message)) or True
    cus.time.sleep = lambda _s: None
    try:
        panes = cus._resume_reactive_slot_sessions("slot-2", config)
    finally:
        cus.live_sessions_on_slot, cus.tmux_send_keys, cus.tmux_send_text, cus.time.sleep = saved
    return panes, texts + keys


def test_resume_message_is_tagged_as_automated():
    _, sent = _capture_resume(_config(reactive={"resume_message": "carry on"}))
    texts = [m for p, m in sent if isinstance(m, str) and "carry on" in m]
    assert texts == [f"{cus.REACTIVE_RESUME_TAG} carry on"], sent
    _, sent = _capture_resume(_config())
    texts = [m for p, m in sent if isinstance(m, str) and "Continue from exactly where" in m]
    assert len(texts) == 1 and texts[0].startswith(cus.REACTIVE_RESUME_TAG)


def test_resume_off_sends_nothing():
    panes, sent = _capture_resume(_config(reactive={"resume_after_slot_swap": False}))
    assert panes == [] and sent == []


# --------------------------------------------------------------------------
# F-F-8: fixed trailing fields survive a comma in the source token
# --------------------------------------------------------------------------

def test_rate_limit_log_comma_in_source_keeps_slot_and_account():
    env = _Env()
    try:
        cus.RATE_LIMIT_LOG.write_text(
            f"{cus.now_iso()},sA,rate_limit_error,mcp__x__do,thing,slot-3,alpha\n"
            f"{cus.now_iso()},sB,rate_limit,stopfailure,slot-4,beta\n"
            f"{cus.now_iso()},sC,rate_limit,legacy4\n")
        entries = cus._read_rate_limit_log_since(None)
        by_sid = {e["session_id"]: e for e in entries}
        assert by_sid["sA"]["slot"] == "slot-3" and by_sid["sA"]["account"] == "alpha"
        assert by_sid["sA"]["source"] == "mcp__x__do,thing"
        assert by_sid["sB"]["slot"] == "slot-4" and by_sid["sB"]["account"] == "beta"
        assert by_sid["sC"]["source"] == "legacy4" and "slot" not in by_sid["sC"]
    finally:
        env.restore()


# --------------------------------------------------------------------------
# Copilot (#187): an unexpected exception re-queues the event before re-raising
# --------------------------------------------------------------------------

def test_slot_move_unexpected_exception_requeues_event():
    env = _Env()
    try:
        slot = env.make_slot("alpha", live=True)
        state = cus.load_state()
        entry = _slot_event(slot, "alpha")
        move = {"slot": slot, "from": "alpha", "to": "beta", "gate": "reactive_429", "tier": 3,
                "deferrable": False, "reason": "test", "pool": "standard", "_reactive_entries": [entry]}
        saved = cus.execute_swap

        def _boom(*a, **k):
            raise PermissionError("creds file locked")

        cus.execute_swap = _boom
        try:
            raised = False
            try:
                cus._execute_slot_moves([move], state, _config(), no_execute=False)
            except PermissionError:
                raised = True
        finally:
            cus.execute_swap = saved
        assert raised, "unexpected exceptions still propagate"
        pending = cus.load_state().get("pending_429_entries") or []
        assert [e["session_id"] for e in pending] == ["sA"], pending
    finally:
        env.restore()


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {name}: {e}")
    sys.exit(1 if failed else 0)
