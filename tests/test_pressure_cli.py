"""Task 21 (spec-2 token-pressure forecaster, STAGE 1): `cus pressure [--json]`
-- the on-demand CALLER click command that performs the Phase-D I/O
(`_pressure_load_state` -> `_read_active_tails(persist=False)` ->
`_parse_sessions_log`/`_session_account_intervals` -> weight fit ->
`_attribute_burn`/`_partition_burn` -> per-session rate/class/trend/rollup ->
`_attribution_confidence`) and hands the results to the PURE `_pressure_snapshot`
(Task 20) as explicit injected products, then only PRINTS.

READ-ONLY, load-bearing (G0/§10.11, FACT #9): this command must never
`save_state`, never mutate the live `state.json`, and never advance the
transcript offset registry (`persist=False`) -- an on-demand caller racing the
daemon's own offset bookkeeping would be a real correctness bug, not just a
style one.

HARNESS (same as every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file; add the repo root to ``sys.path`` and
``import cus``. Monkeypatch the module-globals `cus.STATE_JSON`/
`cus.CONFIG_YAML`/`cus.CLAUDE_DIR`/`cus.SESSIONS_LOG` to an isolated tmp tree
so no test depends on real credentials/tmux/transcripts -- `CLAUDE_DIR` points
at a tmp dir with no ``projects/`` subdir (`_read_active_tails` -> ``{}``
safely) and `SESSIONS_LOG` at a nonexistent file (`_parse_sessions_log` ->
``[]`` safely), so every test's Phase-D I/O is real but empty/quiescent --
pressure level swings come purely from `state.json`'s own
``current_5h_pct``/``current_7d_pct`` vs. the configured gate (Task 2's own
FACT #1/#7), with zero dependency on live transcripts/tmux.

Run: ``python3 -m pytest tests/test_pressure_cli.py -q``.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402
from click.testing import CliRunner  # noqa: E402


# reference_x pinned to 5 (the live production pin, FACT #4); one 20x account
# ("A") gives ratio 4.0 -- the same fleet shape `tests/test_pressure_json.py`
# and `tests/test_pressure_readonly.py` use.
BASE_CFG = {
    "capacity_aware": {"enabled": True, "reference_x": 5},
    "thresholds": {"steps": [70, 85, 94]},   # gate_5h = 94 (live ladder top)
    "per_model_weekly": {"cap_pct": 95},
    "accounts": [{"name": "A", "capacity_x": 20}],
}


def _acct(pct=50.0, pct7d=10.0):
    return {"capacity_x": 20, "current_5h_pct": pct, "current_7d_pct": pct7d}


def _env(tmp_path, monkeypatch, *, state=None, config=None):
    """Wire cus's module-global paths at an isolated tmp tree (same pattern
    `tests/test_pressure_readonly.py`'s `_write_state` uses, extended to also
    cover config.yaml/CLAUDE_DIR/SESSIONS_LOG -- the additional I/O this CLI
    command performs that the pure-snapshot/readonly tests don't touch)."""
    state = state if state is not None else {"accounts": {"A": _acct()}}
    config = config if config is not None else BASE_CFG

    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    monkeypatch.setattr(cus, "STATE_JSON", state_path)

    config_path = tmp_path / "config.yaml"
    cus.write_yaml(config_path, config)
    monkeypatch.setattr(cus, "CONFIG_YAML", config_path)

    # No "projects" subdir -> _read_active_tails/_pressure_transcript_paths
    # safely return {} / [] (Task 10's own documented missing-dir contract).
    monkeypatch.setattr(cus, "CLAUDE_DIR", tmp_path / "claude_home")
    # Nonexistent -> _parse_sessions_log() safely returns [].
    monkeypatch.setattr(cus, "SESSIONS_LOG", tmp_path / "sessions.log")

    # Task 27b: `cmd_pressure` now reads (never writes, persist=False) the
    # daemon's persisted cross-cycle history stores under `PRESSURE_ROOT` --
    # isolate that tree too (same pattern `tests/test_pressure_shadow.py`'s
    # own `_env` uses), otherwise a run here would read the REAL machine's
    # `~/claude-accounts/pressure/` store instead of a clean, empty one.
    accounts_dir = tmp_path / "claude-accounts"
    monkeypatch.setattr(cus, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(cus, "PRESSURE_JSON", accounts_dir / "pressure.json")
    monkeypatch.setattr(cus, "PRESSURE_ROOT", accounts_dir / "pressure")

    return state_path, config_path


# ============================ test_json_flag_valid_snapshot ============================

def test_json_flag_valid_snapshot(tmp_path, monkeypatch):
    """`cus pressure --json` exits 0 and prints ONE valid JSON object shaped
    exactly like `_pressure_snapshot`'s pinned schema (Task 20)."""
    _env(tmp_path, monkeypatch)

    result = CliRunner().invoke(cus.cli, ["pressure", "--json"])
    assert result.exit_code == 0, result.output

    snap = json.loads(result.output)
    for key in ("level", "generated_at", "reference_x", "horizon_min", "pool",
                "accounts", "binding", "episode_id", "weight_fit",
                "safety_factor", "attribution", "sessions"):
        assert key in snap, f"missing top-level key {key!r}: {snap}"

    assert snap["level"] in ("ok", "elevated", "critical")
    assert snap["reference_x"] == pytest.approx(5.0)
    for window in ("5h", "7d"):
        assert window in snap["pool"]
    assert "A" in snap["accounts"]
    assert snap["sessions"] == []  # no sessions.log / transcripts in this env


# ============================ test_readonly_no_state_write ============================

def test_readonly_no_state_write(tmp_path, monkeypatch):
    """`save_state` is never called; `state.json`'s bytes+mtime are unchanged;
    and the offset registry the command builds is genuinely never advanced
    (`_read_active_tails` is called with `persist=False`, and its `offsets`
    argument is left untouched -- the exact G0/§3 invariant this command must
    hold as an on-demand caller that cannot race the daemon)."""
    state_path, _cfg_path = _env(tmp_path, monkeypatch)

    before_bytes = state_path.read_bytes()
    before_mtime = state_path.stat().st_mtime_ns

    def _boom_save(*_a, **_k):
        raise AssertionError("save_state called on the read-only 'cus pressure' path")
    monkeypatch.setattr(cus, "save_state", _boom_save)

    real_read_tails = cus._read_active_tails
    captured = {}

    def _spy_read_tails(now, cfg, offsets, *, persist):
        captured["persist"] = persist
        captured["offsets_before"] = dict(offsets)
        result = real_read_tails(now, cfg, offsets, persist=persist)
        captured["offsets_after"] = dict(offsets)
        return result
    monkeypatch.setattr(cus, "_read_active_tails", _spy_read_tails)

    result = CliRunner().invoke(cus.cli, ["pressure", "--json"])
    assert result.exit_code == 0, result.output

    assert captured["persist"] is False
    assert captured["offsets_before"] == {}
    assert captured["offsets_after"] == {}, "offsets registry must never be advanced (persist=False)"

    assert state_path.read_bytes() == before_bytes
    assert state_path.stat().st_mtime_ns == before_mtime


# ============================ test_live_state_byte_identical ============================

def test_live_state_byte_identical(tmp_path, monkeypatch):
    """A full `cus pressure` compute (not just --json) leaves the on-disk
    state.json byte-identical, AND a fresh `cus.load_state()` after the
    command returns an object deep-equal to the state read before it ran --
    the G0 guarantee that this on-demand path never contaminates the
    daemon's own live state, proven both at the file level and at the
    loaded-object level."""
    state = {"accounts": {"A": _acct(pct=60.0)}, "swap_history": [], "active": "A"}
    state_path, _cfg_path = _env(tmp_path, monkeypatch, state=state)

    before_bytes = state_path.read_bytes()
    before_loaded = cus.load_state()

    result = CliRunner().invoke(cus.cli, ["pressure"])  # default (table) render too
    assert result.exit_code == 0, result.output

    assert state_path.read_bytes() == before_bytes
    after_loaded = cus.load_state()
    assert after_loaded == before_loaded


# ============================ test_table_renders_when_nonok ============================

def test_table_renders_when_nonok(tmp_path, monkeypatch):
    """A breach state (pct over the 5h gate) renders a non-"ok" level in the
    default (non-`--json`) table output -- proves the table path actually
    reflects `_pressure_snapshot`'s computed level, not a static/placeholder
    render."""
    state = {"accounts": {"A": _acct(pct=96.0, pct7d=10.0)}}  # 96 > gate 94
    _env(tmp_path, monkeypatch, state=state)

    result = CliRunner().invoke(cus.cli, ["pressure"])
    assert result.exit_code == 0, result.output
    assert "CRITICAL" in result.output or "ELEVATED" in result.output, result.output
    # sanity: the JSON path on the SAME state agrees on the level word used.
    json_result = CliRunner().invoke(cus.cli, ["pressure", "--json"])
    snap = json.loads(json_result.output)
    assert snap["level"] != "ok"
    assert snap["level"].upper() in result.output


# ==================== test_capacity_model_used_with_gate_off ====================

def test_capacity_model_used_with_gate_off(tmp_path, monkeypatch):
    """The capacity/reference-unit model is read (ratio = capacity_x /
    reference_x, e.g. 4.0 for a 20x account against reference_x=5) REGARDLESS
    of `capacity_aware.enabled` -- disabling the gate must NEVER force ratios
    to a neutral 1.0. Verified by comparing `remaining_units` for a
    `capacity_x=20` account against what a forced ratio=1.0 bug would report:
    with pct=50, gate=94, the true (ratio=4.0) remaining_units is
    `(94-50)/100*4.0 = 1.76`; a ratio=1.0 bug would instead publish `0.44`."""
    cfg_gate_off = dict(BASE_CFG, capacity_aware={"enabled": False, "reference_x": 5})
    state = {"accounts": {"A": _acct(pct=50.0, pct7d=10.0)}}
    _env(tmp_path, monkeypatch, state=state, config=cfg_gate_off)

    result = CliRunner().invoke(cus.cli, ["pressure", "--json"])
    assert result.exit_code == 0, result.output
    snap = json.loads(result.output)

    assert snap["reference_x"] == pytest.approx(5.0)
    assert snap["accounts"]["A"]["capacity_x"] == pytest.approx(20.0)
    remaining = snap["accounts"]["A"]["5h"]["remaining_units"]
    assert remaining == pytest.approx(1.76, abs=1e-6), (
        f"expected ratio-4.0 remaining_units 1.76 (capacity model still applied "
        f"with capacity_aware.enabled=False), got {remaining!r} "
        f"(0.44 would mean the ratio was wrongly forced to 1.0)"
    )


# ==================== test_snapshot_receives_phase_d_products ====================

def test_snapshot_receives_phase_d_products(tmp_path, monkeypatch):
    """The command actually PERFORMS Phase-D I/O and passes the real
    `partition`/`session_table`/`weight_fit`/`attribution` products into
    `_pressure_snapshot` (never `(state, config, now)` alone) -- proven by
    monkeypatching `_pressure_snapshot` itself to a spy that captures its
    kwargs, then asserting all four are present and correctly shaped."""
    _env(tmp_path, monkeypatch)

    captured = {}
    real_snapshot = cus._pressure_snapshot

    def _spy_snapshot(state, config, now, *, partition, session_table,
                       weight_fit, attribution, episode_id=None):
        captured["partition"] = partition
        captured["session_table"] = session_table
        captured["weight_fit"] = weight_fit
        captured["attribution"] = attribution
        return real_snapshot(state, config, now, partition=partition,
                             session_table=session_table, weight_fit=weight_fit,
                             attribution=attribution, episode_id=episode_id)
    monkeypatch.setattr(cus, "_pressure_snapshot", _spy_snapshot)

    result = CliRunner().invoke(cus.cli, ["pressure", "--json"])
    assert result.exit_code == 0, result.output

    assert "partition" in captured and captured["partition"] is not None
    assert hasattr(captured["partition"], "pinned_burn_units")
    assert hasattr(captured["partition"], "rotatable_burn_units")

    assert "session_table" in captured
    assert isinstance(captured["session_table"], list)

    assert "weight_fit" in captured and captured["weight_fit"] is not None
    wf = captured["weight_fit"]
    for key in ("weights", "source", "condition_number", "residual_fraction", "n_windows"):
        assert key in wf, f"weight_fit missing {key!r}: {wf}"
    # no accumulated >=200-window regression history on this on-demand path
    # (documented, not a bug) -> naturally falls to the seed-weight fallback.
    assert wf["source"] == "insufficient-data"

    assert "attribution" in captured and captured["attribution"] is not None
    attribution = captured["attribution"]
    for key in ("confidence", "blindness", "residual_fraction", "reason"):
        assert key in attribution, f"attribution missing {key!r}: {attribution}"



# ==================== test_pressure_json_stdout_is_clean_json ====================

def test_pressure_json_stdout_is_clean_json(tmp_path, monkeypatch):
    """THE critical post-deploy regression: `_log_launch_time_fallback`'s
    greppable line must NEVER land on stdout, or it corrupts `cus pressure
    --json`'s output for every stdout-JSON consumer (statusline,
    `--shadow-report`-adjacent tooling, any scripted caller doing
    `json.loads` on stdout). Launch-time is the UNIVERSAL case today (FACT
    #5 -- no rotation rows in sessions.log), so a real session with a real
    transcript line WILL hit the fallback and WOULD have printed
    `pressure.attribution.launch_time_fallback ...` straight into stdout
    ahead of the JSON, breaking `json.loads`. This builds exactly that
    session (one sessions.log row -> single interval -> launch-time; one
    real assistant transcript line so `_attribute_burn` genuinely drives
    the join) and asserts stdout is nothing but parseable JSON while the
    fallback line lands on stderr instead."""
    _env(tmp_path, monkeypatch)

    now = datetime.now(timezone.utc)
    session_id = "sFallback"
    account = "A"

    def _iso(dt: datetime) -> str:
        return dt.isoformat().replace("+00:00", "Z")

    # sessions.log: ONE row for this session (FACT #5 shape, no rotation
    # rows) -> `_session_account_intervals` yields a single interval ->
    # `_join_usage_account` always degrades to launch-time for it.
    sessions_log = tmp_path / "sessions.log"
    launch_ts = _iso(now - timedelta(minutes=5))
    sessions_log.write_text(
        f"{launch_ts},{session_id},{account},%1,/tmp/tmux-1000/default,/home/yaz/project\n"
    )
    monkeypatch.setattr(cus, "SESSIONS_LOG", sessions_log)

    # A real, recently-modified transcript with a real assistant usage line
    # -> `_read_active_tails` picks it up and `_attribute_burn` actually
    # calls `_join_usage_account` on it (not just an empty-tails no-op).
    projects_dir = cus.CLAUDE_DIR / "projects" / "-home-yaz-project"
    projects_dir.mkdir(parents=True)
    transcript = projects_dir / f"{session_id}.jsonl"
    line = {
        "type": "assistant",
        "uuid": "uid-1",
        "sessionId": session_id,
        "timestamp": _iso(now - timedelta(minutes=1)),
        "message": {
            "usage": {"input_tokens": 100, "output_tokens": 10},
            "model": "claude-opus-4-8",
        },
    }
    transcript.write_text(json.dumps(line) + "\n")

    result = CliRunner(mix_stderr=False).invoke(cus.cli, ["pressure", "--json"])
    assert result.exit_code == 0, result.output

    # stdout must be ONLY the JSON snapshot -- parseable, and no fallback
    # text leaked into it.
    snap = json.loads(result.stdout)
    assert snap["level"] in ("ok", "elevated", "critical")
    assert "launch_time_fallback" not in result.stdout
    assert "pressure.attribution" not in result.stdout

    # The fallback line must have genuinely fired (not merely absent
    # because nothing happened) -- on stderr, naming the session.
    assert "pressure.attribution.launch_time_fallback" in result.stderr
    assert f"session_id={session_id}" in result.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
