"""Tests for GH #36 — statusline shows whether THIS tmux pane is pinned,
and to which account.

Pins live in config.yaml under session_locks.pinned, keyed by tmux pane id
("%12") or Claude session id. The statusline process inherits TMUX_PANE and
CLAUDE_CODE_SESSION_ID from the pane's claude process, so it can answer "am I
pinned?" locally. The badge is `📌<account>`, or `📌<account>!` when the pin
target differs from the account the pane is actually displayed on — the
important divergence case, since in background-swap mode a pin only blocks
daemon relaunches, it does not keep the pane's usage on the pinned account.

Run standalone:  python3 tests/test_statusline_pin.py
Or under pytest: pytest tests/test_statusline_pin.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _pin_config(pinned: dict) -> dict:
    return {"session_locks": {"pinned": pinned}}


class _EnvVars:
    """Set/unset the two env vars the pin lookup reads, with restore().
    Plain setattr style (no pytest fixtures) so the file runs standalone."""

    def __init__(self, tmux_pane=None, session_id=None):
        self._saved = {k: os.environ.get(k) for k in ("TMUX_PANE", "CLAUDE_CODE_SESSION_ID")}
        for key, val in (("TMUX_PANE", tmux_pane), ("CLAUDE_CODE_SESSION_ID", session_id)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# _current_pane_pin — lookup precedence
# ---------------------------------------------------------------------------

def test_pin_matched_by_tmux_pane():
    env = _EnvVars(tmux_pane="%7")
    try:
        key, acct = cus._current_pane_pin(_pin_config({"%7": "merkos"}))
        assert (key, acct) == ("%7", "merkos")
    finally:
        env.restore()


def test_pin_matched_by_session_id():
    env = _EnvVars(tmux_pane="%7", session_id="sess-123")
    try:
        key, acct = cus._current_pane_pin(_pin_config({"sess-123": "default"}))
        assert (key, acct) == ("sess-123", "default")
    finally:
        env.restore()


def test_pane_pin_wins_over_session_pin():
    """Same precedence as session_is_pinned (pane checked first) so the
    daemon's skip-swap decision and the statusline badge can't disagree."""
    env = _EnvVars(tmux_pane="%7", session_id="sess-123")
    try:
        _, acct = cus._current_pane_pin(_pin_config({"%7": "merkos", "sess-123": "default"}))
        assert acct == "merkos"
    finally:
        env.restore()


def test_no_pin_when_pane_not_listed():
    env = _EnvVars(tmux_pane="%9", session_id="sess-999")
    try:
        assert cus._current_pane_pin(_pin_config({"%7": "merkos"})) == (None, None)
    finally:
        env.restore()


def test_no_pin_outside_tmux_without_session():
    env = _EnvVars()   # neither env var present
    try:
        assert cus._current_pane_pin(_pin_config({"%7": "merkos"})) == (None, None)
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# _sl_pin_label — badge formatting
# ---------------------------------------------------------------------------

def test_label_empty_when_unpinned():
    assert cus._sl_pin_label(None, "default", color_on=False) == ""


def test_label_plain_when_pin_matches_active():
    assert cus._sl_pin_label("merkos", "merkos", color_on=False) == "📌merkos"


def test_label_flags_mismatch():
    """Pin says merkos but the pane is shown on default → `!` suffix so the
    divergence is visible even with color off."""
    assert cus._sl_pin_label("merkos", "default", color_on=False) == "📌merkos!"


# ---------------------------------------------------------------------------
# statusline command end-to-end (compact + verbose)
# ---------------------------------------------------------------------------

class _StatuslineEnv:
    """Throwaway state.json + config.yaml with cus path constants repointed,
    plus a quiet diagnose() so the SOS/warning early-returns don't preempt
    the normal output paths under test."""

    def __init__(self, pinned: dict, active: str = "default", verbose: bool = False):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        state_path = root / "state.json"
        state_path.write_text(json.dumps({
            "active": active,
            "accounts": {
                "default": {"current_5h_pct": 10, "current_7d_pct": 5, "next_swap_at_pct": 50},
                "merkos": {"current_5h_pct": 20, "current_7d_pct": 8, "next_swap_at_pct": 50},
            },
        }))
        config_path = root / "config.yaml"
        config_path.write_text(
            "statusline:\n"
            f"  verbose: {'true' if verbose else 'false'}\n"
            "  color: false\n"                 # plain text = assertable output
            "session_locks:\n"
            "  pinned:\n"
            + "".join(f"    '{k}': {v}\n" for k, v in pinned.items())
        )
        self._saved = {k: getattr(cus, k) for k in ("STATE_JSON", "CONFIG_YAML", "diagnose")}
        cus.STATE_JSON = state_path
        cus.CONFIG_YAML = config_path
        cus.diagnose = lambda state, config: []

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def test_compact_statusline_shows_pin_badge():
    envv = _EnvVars(tmux_pane="%3")
    envs = _StatuslineEnv(pinned={"%3": "default"})
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--compact"]).output
        assert "📌default" in out
        assert "!" not in out                  # pin matches shown account
    finally:
        envs.restore()
        envv.restore()


def test_compact_statusline_flags_pin_mismatch():
    """Pane pinned to merkos while the machine (and thus the pane, in
    background-swap mode) is on default → badge must carry the mismatch mark."""
    envv = _EnvVars(tmux_pane="%3")
    envs = _StatuslineEnv(pinned={"%3": "merkos"})
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--compact"]).output
        assert "📌merkos!" in out
    finally:
        envs.restore()
        envv.restore()


def test_compact_statusline_no_badge_when_unpinned():
    envv = _EnvVars(tmux_pane="%9")
    envs = _StatuslineEnv(pinned={"%3": "merkos"})
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--compact"]).output
        assert "📌" not in out
    finally:
        envs.restore()
        envv.restore()


def test_verbose_statusline_shows_pin_badge():
    envv = _EnvVars(tmux_pane="%3")
    envs = _StatuslineEnv(pinned={"%3": "default"}, verbose=True)
    try:
        out = CliRunner().invoke(cus.statusline_cmd, ["--verbose"]).output
        assert "📌default" in out
    finally:
        envs.restore()
        envv.restore()


def _run_all() -> int:
    import types
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
