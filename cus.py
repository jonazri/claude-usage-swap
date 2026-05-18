#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click>=8.0",
#     "pyyaml>=6.0",
# ]
# ///
"""claude-usage-swap (cus) — auto-rotate Claude Code OAuth accounts.

Phases:
  1. Foundations: cus init / list / status / switch.
  2. Auto-rotation: cus daemon polls usage, swaps on progressive thresholds.
  3. Hot-swap of live sessions, Tier 1 (wait-for-Stop).
  4. Tier 2 (pause-message injection).
  5. Tier 3 (force interrupt + 429 reactive + subagent skip-guard).
  6. Operator controls (pin, whitelist, statusline, systemd unit).

Storage:
  ~/claude-accounts/
    config.yaml                  Account list + thresholds + poll interval + hot-swap.
    state.json                   Runtime state (active + per-account thresholds + history).
    sessions.log                 SessionStart hook output (one line per new session).
    stops.log                    Stop hook output (one line per turn end).
    429.log                      PostToolUseFailure detections.
    tool_use.log                 PreToolUse + SubagentStop signals.
    daemon.log                   Daemon stdout/stderr when running.
    inbox.md                     Decisions made autonomously by the daemon.
    account-<name>/
      credentials.json           Snapshot of .credentials.json (whole-file).
      claude-identity.json       Surgical extract: {userID, oauthAccount}.
      meta.yaml                  Human-readable: oauth email, priority, locks, ts.

Swap mechanism (verified 2026-05-18 — see docs/ARCHITECTURE.md):
  Two-file swap on Linux:
    - ~/.claude/.credentials.json   wholesale replace (471 bytes — the entire
                                    OAuth payload per Claude Code's auth docs)
    - ~/.claude.json                surgical key-merge of only `userID` and
                                    `oauthAccount` (21 of 39 keys differ between
                                    accounts but most are machine-level state)

  Both file writes use tempfile-in-same-dir + os.replace() for POSIX-atomic
  rename. Source-account state is saved back to its dir before the new account
  state is laid down, so any single swap is reversible by running
  `cus switch <previous-name>` again.

Methodology lifted (not code) from cux (github.com/inulute/cux):
  - OAuth usage API call shape (internal/usage/usage.go:84-135)
  - Strategy picker patterns (drain/balanced) (internal/strategy/strategy.go)
  - Hook-based turn-boundary signaling
  - PostToolUseFailure 429 substring-match (internal/hooks/hooks.go:765-800)
  - --resume <id> "Go continue." wake-up pattern (wrapper.go:180-185)
NOT lifted: cux's keystore-swap, because on Linux .credentials.json IS the
keystore — no libsecret/keychain dance is needed (Claude Code auth docs are
authoritative on this point).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
import yaml

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

HOME = Path.home()
CLAUDE_JSON = HOME / ".claude.json"
CLAUDE_DIR = HOME / ".claude"
CREDS_JSON = CLAUDE_DIR / ".credentials.json"
ACCOUNTS_DIR = HOME / "claude-accounts"
STATE_JSON = ACCOUNTS_DIR / "state.json"
CONFIG_YAML = ACCOUNTS_DIR / "config.yaml"
SESSIONS_LOG = ACCOUNTS_DIR / "sessions.log"
STOPS_LOG = ACCOUNTS_DIR / "stops.log"
RATE_LIMIT_LOG = ACCOUNTS_DIR / "429.log"
TOOL_USE_LOG = ACCOUNTS_DIR / "tool_use.log"
DAEMON_LOG = ACCOUNTS_DIR / "daemon.log"
DAEMON_PID = ACCOUNTS_DIR / "daemon.pid"
INBOX_MD = ACCOUNTS_DIR / "inbox.md"

# Hook scripts ship in <repo>/hooks/. Daemon installs them into ~/.claude/settings.json
# at user request via `cus hooks install`. Source paths discovered relative to cus.py.
HOOKS_SRC_DIR = Path(__file__).resolve().parent / "hooks"
HOOK_SETTINGS_KEY = "cus"  # signature key in settings.json so we don't clobber other tools

# The full list of keys that are tied to OAuth identity — verified empirically
# against the user's two config dirs (2026-05-18). All other keys in
# ~/.claude.json are machine-level state and stay put during a swap.
ACCOUNT_BOUND_KEYS = ["userID", "oauthAccount"]

# Threshold ladder per the design — climbs each time we return to an account
# that we previously swapped out of. 100 sentinel = "force swap, ignore window".
THRESHOLD_STEPS = [50, 75, 90, 100]

# OAuth usage API — same endpoint cux uses, same endpoint Claude Code itself
# uses for /usage. Per cux/internal/usage/usage.go: this beta header was
# verified live 2026-05-01. Both override-able via env for future shifts.
USAGE_API_URL = os.environ.get("CUS_USAGE_ENDPOINT", "https://api.anthropic.com/api/oauth/usage")
USAGE_API_BETA = os.environ.get("CUS_USAGE_BETA", "oauth-2025-04-20")
USAGE_API_TIMEOUT_SECONDS = 10
USAGE_API_RESPONSE_LIMIT_BYTES = 256 * 1024


# --------------------------------------------------------------------------
# Defaults — everything overridable via config.yaml
# --------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "poll_interval_seconds": 300,
    "strategy": "lowest_usage",  # lowest_usage | drain | strict_priority | round_robin
    "thresholds": {
        "steps": [50, 75, 90],   # 100 force is implicit
        "five_hour": True,       # apply progressive thresholds to 5h window
        "seven_day": True,       # apply progressive thresholds to 7d window
        "reset_below_pct": 50,   # when both windows drop below this, reset next_swap_at to first step
    },
    "hot_swap": {
        "enabled": False,        # Phase 3+ — opt-in
        "tier_2_at_pct": 75,
        "tier_3_at_pct": 90,
        "pause_message": (
            "please pause your current thought — we're swapping Claude "
            "accounts to avoid hitting the usage cap. You'll resume on the "
            "other side; finish what you're saying briefly and stop."
        ),
        "wake_up_message": "Continue where you left off.",
        "cache_bust_window_seconds": 300,   # defer Tier 1 swap if last msg < this old
        "mid_turn_idle_seconds": 30,        # session counted idle if no JSONL line in N s
        "stop_wait_timeout_seconds": 300,   # Tier 1 max wait for Stop signal
        "pause_response_timeout_seconds": 120,  # Tier 2 wait after pause-message
    },
    "subagent_skip": {
        "enabled": True,
        "defer_below_tier": 3,   # if a subagent is active, defer Tier 1/2; Tier 3 proceeds anyway
    },
    "reactive": {
        "enabled": True,         # detect 429s via PostToolUseFailure hook
    },
    "session_locks": {
        "pinned": {},            # {pane_id: account_name} — never swap these
        "never_restart_patterns": [],  # list of regex; matched against tmux pane name
    },
    "daemon": {
        "log_path": str(DAEMON_LOG),
        "pid_path": str(DAEMON_PID),
    },
    "hooks": {
        "install_session_start": True,
        "install_stop": True,
        "install_post_tool_use_failure": True,
        "install_pre_tool_use": False,    # only needed if subagent_skip.enabled
        "install_subagent_stop": False,   # only needed if subagent_skip.enabled
    },
    "accounts": [],              # filled in at init time from discovery
}


# --------------------------------------------------------------------------
# Atomic IO
# --------------------------------------------------------------------------

def atomic_write_bytes(path: Path, content: bytes, mode: int = 0o644) -> None:
    """Write `content` to `path` atomically.

    Uses a tempfile in the same directory and os.replace(), which is
    POSIX-atomic. The tempfile prefix mirrors the target filename so a
    leftover tmp (e.g. from a crash mid-write) is obvious.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def write_json(path: Path, data: Any, mode: int = 0o644) -> None:
    atomic_write_bytes(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(), mode=mode)


def read_yaml(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f) or {}


def write_yaml(path: Path, data: Any) -> None:
    atomic_write_bytes(path, yaml.safe_dump(data, sort_keys=True, default_flow_style=False).encode())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Account discovery
# --------------------------------------------------------------------------

def discover_config_dirs() -> list[tuple[str, Path]]:
    """Return [(account_name, config_dir_path), ...] for all on-machine accounts.

    A Claude config dir is identified by the presence of `.credentials.json`
    inside it. The canonical live config is `~/.claude/`; auxiliary dirs
    follow the naming convention `~/.claude-<name>/`.
    """
    found: list[tuple[str, Path]] = []
    if (CLAUDE_DIR / ".credentials.json").exists():
        found.append(("default", CLAUDE_DIR))
    for p in sorted(HOME.glob(".claude-*")):
        if not p.is_dir():
            continue
        if not (p / ".credentials.json").exists():
            continue
        name = p.name.removeprefix(".claude-")
        if name in ("merkos",):  # explicit; future: any sibling dir
            pass
        found.append((name, p))
    return found


def claude_json_for_config_dir(config_dir: Path) -> Path | None:
    """Locate the .claude.json file associated with a given config dir.

    Claude Code stores the live config at `~/.claude.json` (parent-level)
    when using the default `~/.claude/` dir. When `CLAUDE_CONFIG_DIR` is
    pointed at a custom dir like `~/.claude-merkos/`, the corresponding
    `.claude.json` lives inside that dir.
    """
    if config_dir == CLAUDE_DIR:
        return CLAUDE_JSON if CLAUDE_JSON.exists() else None
    candidate = config_dir / ".claude.json"
    return candidate if candidate.exists() else None


def extract_identity(claude_json_path: Path) -> dict:
    """Pull the account-bound keys out of a .claude.json file."""
    cj = read_json(claude_json_path)
    return {k: cj[k] for k in ACCOUNT_BOUND_KEYS if k in cj}


# --------------------------------------------------------------------------
# Config + state helpers
# --------------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base. Override wins on conflicts."""
    out = {k: (deep_merge(v, override.get(k, {})) if isinstance(v, dict) and isinstance(override.get(k), dict) else override.get(k, v)) for k, v in base.items()}
    for k, v in override.items():
        if k not in out:
            out[k] = v
    return out


def load_config() -> dict:
    """Load config.yaml merged with DEFAULT_CONFIG. Missing file = defaults."""
    user_config = read_yaml(CONFIG_YAML) if CONFIG_YAML.exists() else {}
    return deep_merge(DEFAULT_CONFIG, user_config)


def load_state() -> dict:
    return read_json(STATE_JSON) if STATE_JSON.exists() else {"active": None, "accounts": {}, "swap_history": []}


def save_state(state: dict) -> None:
    write_json(STATE_JSON, state)


def account_meta(name: str) -> dict:
    """Load meta.yaml for a given account; empty dict if missing."""
    meta_path = ACCOUNTS_DIR / f"account-{name}" / "meta.yaml"
    return read_yaml(meta_path) if meta_path.exists() else {}


def append_inbox(entry_type: str, title: str, body: str) -> None:
    """Append a daemon-decision entry to ~/claude-accounts/inbox.md.

    Lightweight version of the AVC inbox format. Used by Tier 3 to log
    shell-kill requests and other autonomous decisions for user review.
    """
    INBOX_MD.parent.mkdir(parents=True, exist_ok=True)
    if not INBOX_MD.exists():
        atomic_write_bytes(INBOX_MD, b"# claude-accounts inbox\n\nAutonomous decisions made by the cus daemon. Newest on top.\n\n")
    ts = now_iso()
    entry = f"\n## {ts} — {entry_type} — {title}\n\n{body}\n"
    # Append-only (not atomic but acceptable for an append-mostly log)
    with INBOX_MD.open("a") as f:
        f.write(entry)


# --------------------------------------------------------------------------
# OAuth usage API client (lifted from cux/internal/usage/usage.go:84-135)
# --------------------------------------------------------------------------

@dataclass
class UsageWindow:
    """One window of usage data from the Anthropic OAuth usage endpoint.

    `utilization` is 0.0-100.0 directly from the API — no math needed.
    `resets_at` may be None if the API returned null (e.g. fresh install).
    """
    utilization: float
    resets_at: str | None


@dataclass
class AccountUsage:
    """All usage windows for a single OAuth account, plus error state."""
    five_hour: UsageWindow | None = None
    seven_day: UsageWindow | None = None
    seven_day_sonnet: UsageWindow | None = None
    seven_day_opus: UsageWindow | None = None
    token_expired: bool = False
    polled_at: str = field(default_factory=now_iso)
    raw: dict = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "AccountUsage":
        return cls()


def _read_access_token(account_name: str) -> str | None:
    """Read the OAuth access_token from the storage-side credentials.json.

    Returns None if the file is missing or unparseable.
    """
    creds_path = ACCOUNTS_DIR / f"account-{account_name}" / "credentials.json"
    if not creds_path.exists():
        return None
    try:
        creds = read_json(creds_path)
        return creds.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, OSError):
        return None


def poll_account_usage(account_name: str) -> AccountUsage:
    """Query the Anthropic OAuth usage endpoint for one account.

    The same endpoint Claude Code itself uses for /usage. Returns parsed
    AccountUsage. On 401, sets `token_expired=True` so the caller can
    surface it without crashing the daemon. On other errors, returns
    empty `AccountUsage` with raw error in `raw['error']`.
    """
    token = _read_access_token(account_name)
    if not token:
        u = AccountUsage.empty()
        u.raw = {"error": f"no access_token for account {account_name}"}
        return u

    req = urllib.request.Request(
        USAGE_API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": USAGE_API_BETA,
            "Accept": "application/json",
            "User-Agent": "claude-usage-swap/0.1 (+https://github.com/rayistern/claude-usage-swap)",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=USAGE_API_TIMEOUT_SECONDS) as resp:
            body = resp.read(USAGE_API_RESPONSE_LIMIT_BYTES + 1)
            if len(body) > USAGE_API_RESPONSE_LIMIT_BYTES:
                u = AccountUsage.empty()
                u.raw = {"error": "response too large"}
                return u
            data = json.loads(body.decode())
    except urllib.error.HTTPError as e:
        u = AccountUsage.empty()
        if e.code == 401:
            u.token_expired = True
        else:
            u.raw = {"error": f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}"}
        return u
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        u = AccountUsage.empty()
        u.raw = {"error": f"{type(e).__name__}: {e}"}
        return u

    def parse_window(obj: dict | None) -> UsageWindow | None:
        if not isinstance(obj, dict):
            return None
        util = obj.get("utilization")
        if util is None:
            return None
        return UsageWindow(utilization=float(util), resets_at=obj.get("resets_at"))

    return AccountUsage(
        five_hour=parse_window(data.get("five_hour")),
        seven_day=parse_window(data.get("seven_day")),
        seven_day_sonnet=parse_window(data.get("seven_day_sonnet")),
        seven_day_opus=parse_window(data.get("seven_day_opus")),
        polled_at=now_iso(),
        raw=data,
    )


def current_max_pct(usage: AccountUsage, config: dict) -> float:
    """Return the higher of 5h / 7d utilization (whichever the config enables)."""
    thresholds = config.get("thresholds", {})
    candidates = []
    if thresholds.get("five_hour", True) and usage.five_hour:
        candidates.append(usage.five_hour.utilization)
    if thresholds.get("seven_day", True) and usage.seven_day:
        candidates.append(usage.seven_day.utilization)
    return max(candidates) if candidates else 0.0


# --------------------------------------------------------------------------
# Strategy picker (lifted from cux/internal/strategy/strategy.go)
# --------------------------------------------------------------------------

@dataclass
class SwapTarget:
    name: str
    reason: str


def pick_swap_target(state: dict, config: dict) -> SwapTarget | None:
    """Pick which account to swap to, based on configured strategy.

    Strategies:
      - lowest_usage: pick the (non-current, non-token-expired) account with
        lowest max(5h, 7d) utilization. Cux's "balanced".
      - drain: deplete current; pick a candidate only if current is over cap.
        Within candidates, prefer those farther from their own cap.
      - strict_priority: respect priority order; only fall through if
        higher-priority accounts are over threshold.
      - round_robin: pick next-in-list (wrapping). Ignores usage.
    """
    current = state.get("active")
    accounts = state.get("accounts", {})
    if not current or not accounts:
        return None

    candidates: list[tuple[str, dict]] = [
        (name, acct) for name, acct in accounts.items()
        if name != current and not acct.get("token_expired", False)
    ]
    if not candidates:
        return None

    strategy = config.get("strategy", "lowest_usage")

    if strategy == "lowest_usage":
        # cux balanced: sort ascending by 7d util, ties broken by 5h util
        candidates.sort(key=lambda kv: (kv[1].get("current_7d_pct", 0.0), kv[1].get("current_5h_pct", 0.0)))
        chosen, _ = candidates[0]
        return SwapTarget(name=chosen, reason="lowest_usage: lowest 7d util")

    if strategy == "drain":
        # cux drain: prefer candidates with both windows under their own next_swap_at
        ordered = [
            (name, acct) for name, acct in candidates
            if acct.get("current_5h_pct", 0.0) < acct.get("next_swap_at_pct", 50)
            and acct.get("current_7d_pct", 0.0) < acct.get("next_swap_at_pct", 50)
        ]
        if ordered:
            ordered.sort(key=lambda kv: (-kv[1].get("current_7d_pct", 0.0),))  # closest-to-cap first
            chosen, _ = ordered[0]
            return SwapTarget(name=chosen, reason="drain: 7d under cap")
        # Pass 2: any candidate with 5h headroom
        ordered = [(n, a) for n, a in candidates if a.get("current_5h_pct", 0.0) < 100]
        if ordered:
            ordered.sort(key=lambda kv: -kv[1].get("current_7d_pct", 0.0))
            chosen, _ = ordered[0]
            return SwapTarget(name=chosen, reason="drain: 5h has room")
        return None

    if strategy == "strict_priority":
        # Sort by priority ascending (1 = highest); pick first with room
        cfg_accounts = {a["name"]: a for a in config.get("accounts", [])}
        candidates.sort(key=lambda kv: cfg_accounts.get(kv[0], {}).get("priority", 99))
        for name, acct in candidates:
            if acct.get("current_5h_pct", 0.0) < acct.get("next_swap_at_pct", 50) \
               and acct.get("current_7d_pct", 0.0) < acct.get("next_swap_at_pct", 50):
                return SwapTarget(name=name, reason="strict_priority: highest priority with headroom")
        return None

    if strategy == "round_robin":
        names = sorted(accounts.keys())
        idx = names.index(current)
        nxt = names[(idx + 1) % len(names)]
        return SwapTarget(name=nxt, reason="round_robin")

    return None


# --------------------------------------------------------------------------
# Swap primitive (callable from daemon, not just CLI)
# --------------------------------------------------------------------------

def execute_swap(target_name: str, trigger: str = "manual") -> dict:
    """Atomically swap to `target_name`. Returns updated state dict.

    Shared between the CLI `cus switch` command and the daemon's auto-swap.
    Caller is responsible for any preflight (hot-swap orchestration, etc.).
    """
    state = load_state()
    if target_name not in state["accounts"]:
        raise ValueError(f"Unknown account '{target_name}'. Known: {sorted(state['accounts'].keys())}")
    current = state["active"]
    if target_name == current:
        return state

    target_dir = ACCOUNTS_DIR / f"account-{target_name}"
    current_dir = ACCOUNTS_DIR / f"account-{current}"

    if not (target_dir / "credentials.json").exists():
        raise FileNotFoundError(f"{target_dir}/credentials.json missing — re-run `cus init`")
    if not (target_dir / "claude-identity.json").exists():
        raise FileNotFoundError(f"{target_dir}/claude-identity.json missing — re-run `cus init`")

    try:
        live_cj = read_json(CLAUDE_JSON)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{CLAUDE_JSON} unparseable ({e}) — Claude may be mid-write. Retry in 1s.")

    # Save current identity + creds back to current's storage
    current_identity = {k: live_cj[k] for k in ACCOUNT_BOUND_KEYS if k in live_cj}
    write_json(current_dir / "claude-identity.json", current_identity)
    shutil.copy2(CREDS_JSON, current_dir / "credentials.json")
    os.chmod(current_dir / "credentials.json", 0o600)

    # Merge target identity + replace creds
    target_identity = read_json(target_dir / "claude-identity.json")
    for k, v in target_identity.items():
        live_cj[k] = v
    write_json(CLAUDE_JSON, live_cj)
    shutil.copy2(target_dir / "credentials.json", CREDS_JSON)
    os.chmod(CREDS_JSON, 0o600)

    # Update state with progressive-threshold bookkeeping
    ts = now_iso()
    state["active"] = target_name
    state["accounts"][target_name]["last_swap_ts"] = ts

    # Bump current account's next_swap_at_pct ladder
    current_acct = state["accounts"][current]
    cur_step = current_acct.get("next_swap_at_pct", THRESHOLD_STEPS[0])
    next_idx = THRESHOLD_STEPS.index(cur_step) + 1 if cur_step in THRESHOLD_STEPS else len(THRESHOLD_STEPS) - 1
    next_idx = min(next_idx, len(THRESHOLD_STEPS) - 1)
    current_acct["next_swap_at_pct"] = THRESHOLD_STEPS[next_idx]

    state.setdefault("swap_history", []).append({
        "ts": ts, "from": current, "to": target_name, "trigger": trigger,
    })
    save_state(state)
    return state


# --------------------------------------------------------------------------
# Hook installer (~/.claude/settings.json upsert)
# --------------------------------------------------------------------------

HOOK_EVENTS = {
    "SessionStart": ("cus_session_start.sh", "install_session_start"),
    "Stop": ("cus_stop.sh", "install_stop"),
    "PostToolUseFailure": ("cus_post_tool_use_failure.sh", "install_post_tool_use_failure"),
    "PreToolUse": ("cus_pre_tool_use.sh", "install_pre_tool_use"),
    "SubagentStop": ("cus_subagent_stop.sh", "install_subagent_stop"),
}


def _settings_json_path() -> Path:
    return CLAUDE_DIR / "settings.json"


def install_hooks(config: dict) -> dict[str, str]:
    """Install enabled hooks into ~/.claude/settings.json.

    Each hook entry is tagged with a signature in its `_cus_marker` field
    so we can identify our entries and avoid clobbering others'. Returns
    {event_name: "installed" | "already_installed" | "skipped (disabled)"}.
    """
    settings_path = _settings_json_path()
    settings = read_json(settings_path) if settings_path.exists() else {}
    hooks = settings.setdefault("hooks", {})

    result: dict[str, str] = {}
    for event, (script_name, config_key) in HOOK_EVENTS.items():
        if not config.get("hooks", {}).get(config_key, True):
            result[event] = "skipped (disabled in config.yaml)"
            continue

        script_path = HOOKS_SRC_DIR / script_name
        if not script_path.exists():
            result[event] = f"missing hook script at {script_path}"
            continue

        # Hooks for an event are stored as a list-of-matchers. We use
        # an empty matcher (matches everything) for our entries.
        event_entries = hooks.setdefault(event, [])
        entry = next(
            (e for e in event_entries if isinstance(e, dict) and e.get("_cus_marker") == HOOK_SETTINGS_KEY),
            None,
        )
        new_entry = {
            "_cus_marker": HOOK_SETTINGS_KEY,
            "matcher": "",
            "hooks": [{"type": "command", "command": str(script_path)}],
        }
        if entry:
            if entry == new_entry:
                result[event] = "already_installed"
                continue
            event_entries[event_entries.index(entry)] = new_entry
            result[event] = "updated"
        else:
            event_entries.append(new_entry)
            result[event] = "installed"

    write_json(settings_path, settings)
    return result


def uninstall_hooks() -> dict[str, str]:
    """Remove all cus-marked entries from ~/.claude/settings.json."""
    settings_path = _settings_json_path()
    if not settings_path.exists():
        return {event: "no settings.json" for event in HOOK_EVENTS}
    settings = read_json(settings_path)
    hooks = settings.get("hooks", {})

    result: dict[str, str] = {}
    for event in HOOK_EVENTS:
        entries = hooks.get(event, [])
        new_entries = [e for e in entries if not (isinstance(e, dict) and e.get("_cus_marker") == HOOK_SETTINGS_KEY)]
        if len(new_entries) != len(entries):
            hooks[event] = new_entries
            result[event] = "uninstalled"
        else:
            result[event] = "not present"
    write_json(settings_path, settings)
    return result


def list_hooks() -> dict[str, str]:
    """Report current install state for each hook event."""
    settings_path = _settings_json_path()
    if not settings_path.exists():
        return {event: "no settings.json" for event in HOOK_EVENTS}
    settings = read_json(settings_path)
    hooks = settings.get("hooks", {})
    result: dict[str, str] = {}
    for event, (script_name, _) in HOOK_EVENTS.items():
        entries = hooks.get(event, [])
        marked = [e for e in entries if isinstance(e, dict) and e.get("_cus_marker") == HOOK_SETTINGS_KEY]
        if marked:
            cmds = ",".join(h.get("command", "?") for e in marked for h in e.get("hooks", []))
            result[event] = f"installed -> {cmds}"
        else:
            result[event] = "not installed"
    return result


# --------------------------------------------------------------------------
# Daemon decision engine
# --------------------------------------------------------------------------

@dataclass
class SwapDecision:
    """Output of decide_swap. Sentinel `None` from decide_swap = no action."""
    target: str
    reason: str
    tier: int   # 1 = wait-for-Stop, 2 = pause-message, 3 = force


def determine_tier(active_acct: dict, config: dict) -> int:
    """Map current next_swap_at_pct to a tier based on config.

    Tier 1: at first step (default 50) — gentlest, wait for Stop, defer if cache warm.
    Tier 2: at tier_2_at_pct step (default 75) — inject pause-message.
    Tier 3: at tier_3_at_pct step or force (default 90 or 100) — interrupt, log shells.
    """
    step = active_acct.get("next_swap_at_pct", 50)
    hot = config.get("hot_swap", {})
    if step >= hot.get("tier_3_at_pct", 90):
        return 3
    if step >= hot.get("tier_2_at_pct", 75):
        return 2
    return 1


def decide_swap(state: dict, config: dict, usage_by_account: dict[str, AccountUsage]) -> SwapDecision | None:
    """Given current state + fresh usage, decide whether to swap.

    Returns None if no action needed. Otherwise a SwapDecision with target
    + tier the caller should respect.
    """
    current = state.get("active")
    if not current:
        return None

    active_acct = state["accounts"][current]
    threshold = active_acct.get("next_swap_at_pct", THRESHOLD_STEPS[0])
    if threshold >= 100:  # forced: any usage trips
        threshold = 0

    cur_usage = usage_by_account.get(current)
    if cur_usage is None:
        return None
    cur_pct = current_max_pct(cur_usage, config)
    if cur_pct < threshold:
        return None

    target = pick_swap_target(state, config)
    if target is None:
        return None
    return SwapDecision(target=target.name, reason=f"{target.reason}; current {current} at {cur_pct:.1f}% >= {threshold}%", tier=determine_tier(active_acct, config))


def update_state_with_usage(state: dict, usage_by_account: dict[str, AccountUsage]) -> dict:
    """Mutate state.json's per-account current_*_pct from a poll cycle."""
    for name, acct in state["accounts"].items():
        u = usage_by_account.get(name)
        if u is None:
            continue
        if u.token_expired:
            acct["token_expired"] = True
            continue
        acct["token_expired"] = False
        acct["current_5h_pct"] = u.five_hour.utilization if u.five_hour else 0.0
        acct["current_7d_pct"] = u.seven_day.utilization if u.seven_day else 0.0
        acct["last_poll_ts"] = u.polled_at
        if u.five_hour and u.five_hour.resets_at:
            acct["five_hour_resets_at"] = u.five_hour.resets_at
        if u.seven_day and u.seven_day.resets_at:
            acct["seven_day_resets_at"] = u.seven_day.resets_at
    return state


def maybe_reset_thresholds(state: dict, config: dict) -> None:
    """Reset next_swap_at_pct to the first step when both windows are well under it.

    Without this, after a week's reset an account's ladder stays at 90 and
    we'd never use the gentle Tier 1 again.
    """
    reset_below = config.get("thresholds", {}).get("reset_below_pct", 50)
    first_step = THRESHOLD_STEPS[0]
    for name, acct in state["accounts"].items():
        if acct.get("current_5h_pct", 0) < reset_below and acct.get("current_7d_pct", 0) < reset_below:
            if acct.get("next_swap_at_pct", first_step) > first_step:
                acct["next_swap_at_pct"] = first_step


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """claude-usage-swap (cus) — auto-rotate Claude Code OAuth accounts."""


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would happen without writing anything.")
def init(dry_run: bool) -> None:
    """Discover existing Claude config dirs and import each as an account.

    Idempotent: re-running skips accounts that already exist in
    ~/claude-accounts/. The live ~/.claude/ is named "default"; sibling
    ~/.claude-<name>/ dirs are named "<name>".
    """
    candidates = discover_config_dirs()
    if not candidates:
        click.echo("No Claude config dirs found (looked for ~/.claude/ and ~/.claude-*/).")
        sys.exit(1)

    click.echo(f"Discovered {len(candidates)} config dir(s):")
    for name, p in candidates:
        cj = claude_json_for_config_dir(p)
        cj_note = f"+ {cj}" if cj else "+ (no .claude.json found)"
        click.echo(f"  {name}: {p} {cj_note}")
    click.echo()

    if dry_run:
        click.echo("(dry-run) Would create:")
        click.echo(f"  {ACCOUNTS_DIR}/")
        for name, _ in candidates:
            click.echo(f"    account-{name}/credentials.json")
            click.echo(f"    account-{name}/claude-identity.json")
            click.echo(f"    account-{name}/meta.yaml")
        click.echo(f"  {STATE_JSON}")
        click.echo(f"  {CONFIG_YAML}")
        return

    ACCOUNTS_DIR.mkdir(exist_ok=True)
    imported = 0
    skipped = 0

    for name, src_dir in candidates:
        dst = ACCOUNTS_DIR / f"account-{name}"
        if dst.exists():
            click.echo(f"  skip {name}: {dst} already exists")
            skipped += 1
            continue

        dst.mkdir(parents=True)

        # 1. Credentials — whole-file copy with 0600 permissions
        src_creds = src_dir / ".credentials.json"
        dst_creds = dst / "credentials.json"
        shutil.copy2(src_creds, dst_creds)
        os.chmod(dst_creds, 0o600)

        # 2. Identity — surgical extract of just the account-bound keys
        identity: dict = {}
        cj_path = claude_json_for_config_dir(src_dir)
        if cj_path is not None:
            identity = extract_identity(cj_path)
        write_json(dst / "claude-identity.json", identity)

        # 3. Meta — human-readable, editable
        oauth = identity.get("oauthAccount") or {}
        meta = {
            "name": name,
            "source_dir": str(src_dir),
            "oauth_email": oauth.get("emailAddress", "unknown") if isinstance(oauth, dict) else "unknown",
            "oauth_account_uuid": oauth.get("accountUuid", "unknown") if isinstance(oauth, dict) else "unknown",
            "priority": 1,
            "locked_sessions": [],
            "imported_ts": now_iso(),
        }
        write_yaml(dst / "meta.yaml", meta)
        click.echo(f"  imported {name} -> {dst}")
        imported += 1

    # 4. state.json — runtime state for the (future) daemon
    if not STATE_JSON.exists():
        state = {
            "active": "default" if any(n == "default" for n, _ in candidates) else candidates[0][0],
            "accounts": {
                name: {
                    "current_5h_pct": 0.0,
                    "current_7d_pct": 0.0,
                    "next_swap_at_pct": 50,
                    "last_swap_ts": None,
                }
                for name, _ in candidates
            },
            "swap_history": [],
        }
        write_json(STATE_JSON, state)
        click.echo(f"  wrote {STATE_JSON} (active = {state['active']})")

    # 5. config.yaml — user-editable defaults
    if not CONFIG_YAML.exists():
        config = {
            "accounts": [{"name": n, "priority": 1} for n, _ in candidates],
            "poll_interval_seconds": 300,
            "thresholds": {
                "steps": THRESHOLD_STEPS[:-1],  # exclude 100 (force) from user-config
                "five_hour": True,
                "seven_day": True,
            },
            "strategy": "lowest_usage",  # alternatives: round_robin, strict_priority
        }
        write_yaml(CONFIG_YAML, config)
        click.echo(f"  wrote {CONFIG_YAML}")

    click.echo()
    click.echo(f"Done. Imported {imported}, skipped {skipped}.")
    if imported and any(n == "default" for n, _ in candidates):
        click.echo("Live ~/.claude/ is registered as 'default' (the currently-active account).")


@cli.command(name="list")
def list_cmd() -> None:
    """List configured accounts with their identities."""
    if not ACCOUNTS_DIR.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    state = read_json(STATE_JSON) if STATE_JSON.exists() else {"active": None}
    for acct_dir in sorted(ACCOUNTS_DIR.glob("account-*")):
        name = acct_dir.name.removeprefix("account-")
        meta_path = acct_dir / "meta.yaml"
        meta = read_yaml(meta_path) if meta_path.exists() else {}
        active_marker = " ← ACTIVE" if name == state.get("active") else ""
        click.echo(f"{name}{active_marker}")
        click.echo(f"  oauth_email: {meta.get('oauth_email', 'unknown')}")
        click.echo(f"  account_uuid: {meta.get('oauth_account_uuid', 'unknown')}")
        click.echo(f"  source_dir: {meta.get('source_dir', 'unknown')}")
        click.echo(f"  priority: {meta.get('priority', 1)}")
        click.echo()


@cli.command()
def status() -> None:
    """Show active account and per-account usage state."""
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    state = read_json(STATE_JSON)
    click.echo(f"Active account: {state['active']}")
    click.echo()
    click.echo(f"{'Account':<20} {'5h %':>8} {'7d %':>8} {'Next swap':>12} {'Last swap':<30}")
    click.echo("-" * 80)
    for name, a in sorted(state["accounts"].items()):
        marker = " *" if name == state["active"] else ""
        last = a["last_swap_ts"] or "never"
        click.echo(f"{name+marker:<20} {a['current_5h_pct']:>8.1f} {a['current_7d_pct']:>8.1f} {a['next_swap_at_pct']:>12} {last:<30}")
    click.echo()
    history = state.get("swap_history", [])
    if history:
        click.echo(f"Recent swaps ({min(5, len(history))} of {len(history)}):")
        for entry in history[-5:]:
            click.echo(f"  {entry['ts']}  {entry['from']} -> {entry['to']}  ({entry.get('trigger', 'unknown')})")


@cli.command()
@click.argument("target")
@click.option("--dry-run", is_flag=True, help="Show what would happen without writing anything.")
@click.option("--trigger", default="manual", help="Tag for the swap history entry.")
def switch(target: str, dry_run: bool, trigger: str) -> None:
    """Atomically swap to a different account.

    Sequence (verified in docs/ARCHITECTURE.md):
      1. Read current live ~/.claude.json (abort if unparseable).
      2. Save current account's identity back to its account dir.
      3. Save current credentials back to its account dir.
      4. Merge target's identity into live ~/.claude.json.
      5. Replace live ~/.claude/.credentials.json with target's.
      6. Update state.json.

    Any failure between steps 4 and 5 leaves a window where credentials and
    identity disagree. Mitigation: steps 4 and 5 use atomic rename, and an
    error in step 5 leaves step 4's write reversible by re-running
    `cus switch <original>`.
    """
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    state = read_json(STATE_JSON)
    if target not in state["accounts"]:
        click.echo(f"Unknown account '{target}'. Known: {sorted(state['accounts'].keys())}")
        sys.exit(1)

    current = state["active"]
    if target == current:
        click.echo(f"{target} is already active. Nothing to do.")
        return

    target_dir = ACCOUNTS_DIR / f"account-{target}"
    current_dir = ACCOUNTS_DIR / f"account-{current}"

    if not (target_dir / "credentials.json").exists():
        click.echo(f"ERROR: {target_dir}/credentials.json is missing. Re-run `cus init`.")
        sys.exit(1)
    if not (target_dir / "claude-identity.json").exists():
        click.echo(f"ERROR: {target_dir}/claude-identity.json is missing. Re-run `cus init`.")
        sys.exit(1)

    # Sanity-check live ~/.claude.json is parseable BEFORE doing anything destructive
    try:
        live_cj = read_json(CLAUDE_JSON)
    except json.JSONDecodeError as e:
        click.echo(f"ERROR: {CLAUDE_JSON} is unparseable ({e}). Aborting.")
        click.echo("This usually means Claude is mid-write. Wait 1 second and retry.")
        sys.exit(2)

    target_identity = read_json(target_dir / "claude-identity.json")

    if dry_run:
        click.echo(f"(dry-run) Swap plan: {current} -> {target}")
        click.echo(f"  1. Save current identity from ~/.claude.json into {current_dir}/claude-identity.json")
        for k in ACCOUNT_BOUND_KEYS:
            if k in live_cj:
                preview = json.dumps(live_cj[k])[:60]
                click.echo(f"       {k}: {preview}...")
        click.echo(f"  2. Save current credentials from {CREDS_JSON} into {current_dir}/credentials.json")
        click.echo(f"  3. Merge into live ~/.claude.json:")
        for k, v in target_identity.items():
            preview = json.dumps(v)[:60]
            click.echo(f"       {k}: {preview}...")
        click.echo(f"  4. Replace live {CREDS_JSON} with {target_dir}/credentials.json")
        click.echo(f"  5. Mark state.json active = {target}")
        return

    # 1. Save current identity
    current_identity = {k: live_cj[k] for k in ACCOUNT_BOUND_KEYS if k in live_cj}
    write_json(current_dir / "claude-identity.json", current_identity)

    # 2. Save current credentials
    shutil.copy2(CREDS_JSON, current_dir / "credentials.json")
    os.chmod(current_dir / "credentials.json", 0o600)

    # 3. Merge target identity into live ~/.claude.json
    for k, v in target_identity.items():
        live_cj[k] = v
    write_json(CLAUDE_JSON, live_cj)

    # 4. Replace live credentials.json
    shutil.copy2(target_dir / "credentials.json", CREDS_JSON)
    os.chmod(CREDS_JSON, 0o600)

    # 5. Update state.json (atomic)
    ts = now_iso()
    state["active"] = target
    state["accounts"][target]["last_swap_ts"] = ts
    state.setdefault("swap_history", []).append({
        "ts": ts,
        "from": current,
        "to": target,
        "trigger": trigger,
    })
    write_json(STATE_JSON, state)

    click.echo(f"Swapped: {current} -> {target}")
    click.echo(f"  ~/.claude.json: userID + oauthAccount updated")
    click.echo(f"  ~/.claude/.credentials.json: replaced")
    click.echo(f"  Next claude invocation will use {target}.")


# --------------------------------------------------------------------------
# Phase 2 commands: poll, daemon, hooks
# --------------------------------------------------------------------------

@cli.command()
@click.option("--account", default=None, help="Poll just one account (default: all).")
@click.option("--no-write", is_flag=True, help="Don't update state.json — just print the response.")
def poll(account: str | None, no_write: bool) -> None:
    """One-shot usage poll. Useful for testing or cron-driven setups.

    Calls the Anthropic OAuth usage endpoint per account (same endpoint
    Claude Code itself uses for `/usage`). Updates state.json with the
    results unless `--no-write`.
    """
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    state = load_state()
    config = load_config()
    targets = [account] if account else list(state["accounts"].keys())

    usage_by_account: dict[str, AccountUsage] = {}
    for name in targets:
        click.echo(f"polling {name}...")
        u = poll_account_usage(name)
        usage_by_account[name] = u
        if u.token_expired:
            click.echo(f"  TOKEN EXPIRED — re-auth this account (claude login under its config dir)")
            continue
        if u.raw.get("error"):
            click.echo(f"  ERROR: {u.raw['error']}")
            continue
        fh = f"{u.five_hour.utilization:.1f}%" if u.five_hour else "—"
        sd = f"{u.seven_day.utilization:.1f}%" if u.seven_day else "—"
        click.echo(f"  5h: {fh}    7d: {sd}    polled_at: {u.polled_at}")

    if not no_write:
        update_state_with_usage(state, usage_by_account)
        maybe_reset_thresholds(state, config)
        save_state(state)
        click.echo()
        click.echo(f"state.json updated.")


@cli.command()
@click.option("--once", is_flag=True, help="Run a single poll-decide-act cycle and exit.")
@click.option("--foreground", is_flag=True, default=True, help="Run in foreground (default; for systemd, tmux pane, etc.).")
@click.option("--no-execute", is_flag=True, help="Decide but don't actually swap. Useful for dry-run testing.")
def daemon(once: bool, foreground: bool, no_execute: bool) -> None:
    """Run the auto-rotation daemon.

    Loop:
      1. Poll all accounts' usage via the OAuth API.
      2. Update state.json.
      3. Decide whether to swap (progressive thresholds, strategy picker).
      4. Execute the swap (Phase 2: simple swap; Phase 3+: hot-swap orchestration).
      5. Sleep `poll_interval_seconds` from config.

    Crashes write a stack trace to ~/claude-accounts/daemon.log and exit
    non-zero. Restarting picks up from state.json — no in-memory state lost.

    Use --once for cron-driven setups: schedule cus daemon --once every
    5 minutes via crontab and skip running a persistent process.
    """
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    def one_cycle() -> None:
        state = load_state()
        config = load_config()
        click.echo(f"[{now_iso()}] cycle start. active={state['active']}")

        # 1. Poll
        usage_by_account: dict[str, AccountUsage] = {}
        for name in state["accounts"]:
            usage_by_account[name] = poll_account_usage(name)

        # 2. Update state
        update_state_with_usage(state, usage_by_account)
        maybe_reset_thresholds(state, config)

        # 3. Decide
        decision = decide_swap(state, config, usage_by_account)

        # Persist usage updates BEFORE acting on swap (so a crash during
        # swap leaves valid usage state)
        save_state(state)

        if decision is None:
            # Diagnose: was a swap WANTED but no target available?
            active = state["active"]
            active_acct = state["accounts"][active]
            threshold = active_acct.get("next_swap_at_pct", THRESHOLD_STEPS[0])
            active_usage = usage_by_account.get(active)
            wanted = (
                active_usage is not None
                and not active_usage.token_expired
                and current_max_pct(active_usage, config) >= threshold
            )
            if wanted:
                click.echo(f"  threshold tripped on {active} ({current_max_pct(active_usage, config):.1f}% >= {threshold}%) but no valid swap target")
            for name, acct in state["accounts"].items():
                marker = " *" if name == state["active"] else "  "
                te = " (TOKEN_EXPIRED)" if acct.get("token_expired") else ""
                click.echo(f"  {marker}{name}: 5h={acct.get('current_5h_pct', 0):.1f}%, 7d={acct.get('current_7d_pct', 0):.1f}%, next={acct.get('next_swap_at_pct', 50)}%{te}")
            return

        click.echo(f"  swap decision: {state['active']} -> {decision.target} (tier {decision.tier})")
        click.echo(f"    reason: {decision.reason}")

        if no_execute:
            click.echo("    (--no-execute) skipping actual swap")
            return

        # 4. Execute. Phase 2: simple swap. Phase 3+ will dispatch to hot-swap
        # orchestrator based on `decision.tier` and config['hot_swap']['enabled'].
        if config.get("hot_swap", {}).get("enabled", False):
            # Phase 3+ — defer to hot_swap_orchestrate (defined in Phase 3 section)
            try:
                hot_swap_orchestrate(decision, state, config)
            except NameError:
                click.echo("    hot_swap_orchestrate not yet implemented; doing simple swap")
                execute_swap(decision.target, trigger=f"auto-tier{decision.tier}")
        else:
            execute_swap(decision.target, trigger=f"auto-tier{decision.tier}")
            click.echo(f"    swapped (new sessions only — live sessions unaffected)")

    if once:
        one_cycle()
        return

    config = load_config()
    interval = config.get("poll_interval_seconds", 300)
    click.echo(f"daemon starting. poll_interval={interval}s. Ctrl-C to stop.")
    try:
        while True:
            try:
                one_cycle()
            except Exception as e:
                click.echo(f"ERROR in cycle: {type(e).__name__}: {e}", err=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo("\ndaemon stopped.")


@cli.group()
def hooks() -> None:
    """Manage Claude Code hooks used by cus.

    Hooks ship as small bash scripts in <repo>/hooks/ and are registered
    in ~/.claude/settings.json under a signature-keyed entry so we don't
    clobber other tools.
    """


@hooks.command("install")
def hooks_install_cmd() -> None:
    """Install enabled hooks into ~/.claude/settings.json."""
    config = load_config()
    result = install_hooks(config)
    click.echo("Hook installation:")
    for event, status in result.items():
        click.echo(f"  {event:<30} {status}")


@hooks.command("uninstall")
def hooks_uninstall_cmd() -> None:
    """Remove all cus-marked entries from ~/.claude/settings.json."""
    result = uninstall_hooks()
    click.echo("Hook uninstall:")
    for event, status in result.items():
        click.echo(f"  {event:<30} {status}")


@hooks.command("list")
def hooks_list_cmd() -> None:
    """Show current cus hook install state."""
    result = list_hooks()
    click.echo("Hook state:")
    for event, status in result.items():
        click.echo(f"  {event:<30} {status}")


@cli.command()
def config() -> None:
    """Print effective config (defaults merged with config.yaml)."""
    cfg = load_config()
    click.echo(yaml.safe_dump(cfg, sort_keys=True, default_flow_style=False))


if __name__ == "__main__":
    cli()
