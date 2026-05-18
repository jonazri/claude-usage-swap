#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click>=8.0",
#     "pyyaml>=6.0",
# ]
# ///
"""claude-usage-swap (cus) — auto-rotate Claude Code OAuth accounts.

Phase 1: foundations.

Commands:
  cus init [--dry-run]           Discover existing Claude config dirs and
                                 import each as an account into ~/claude-accounts/.
  cus status                     Show active account and per-account state.
  cus list                       List configured accounts with their identities.
  cus switch <name> [--dry-run]  Atomically swap the active account.

Storage:
  ~/claude-accounts/
    config.yaml                  Account list + thresholds + poll interval (Phase 2+).
    state.json                   Runtime state (active + per-account thresholds).
    account-<name>/
      credentials.json           Snapshot of .credentials.json (whole-file).
      claude-identity.json       Surgical extract: {userID, oauthAccount}.
      meta.yaml                  Human-readable: oauth email, priority, locks, ts.

Swap mechanism (verified 2026-05-18 — see docs/ARCHITECTURE.md):
  Two-file swap on Linux:
    - ~/.claude/.credentials.json   wholesale replace (471 bytes, the entire
                                    OAuth payload — per Claude Code's auth docs)
    - ~/.claude.json                surgical key-merge of only `userID` and
                                    `oauthAccount` (21 of 39 keys differ between
                                    accounts but most are machine-level
                                    state — mcpServers, projects, caches —
                                    that we deliberately do NOT swap)

  Both file writes use tempfile-in-same-dir + os.replace() for POSIX-atomic
  rename. Source-account state is saved back to its dir before the new account
  state is laid down, so any single swap is reversible by running
  `cus switch <previous-name>` again.

Lifted methodology (not code) from cux (github.com/inulute/cux):
  - Hook-based turn-boundary signaling (Phase 2+)
  - PostToolUseFailure 429 substring-match (Phase 5)
  - --resume <id> "Go continue." wake-up pattern (Phase 3)
NOT lifted: cux's keystore-swap (~/internal/switcher/switcher.go) because on
Linux .credentials.json IS the keystore — no libsecret/keychain dance is
needed (Claude Code auth docs are authoritative on this point).
"""

import json
import os
import shutil
import sys
import tempfile
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

# The full list of keys that are tied to OAuth identity — verified empirically
# against the user's two config dirs (2026-05-18). All other keys in
# ~/.claude.json are machine-level state and stay put during a swap.
ACCOUNT_BOUND_KEYS = ["userID", "oauthAccount"]

# Threshold ladder per the design — climbs each time we return to an account
# that we previously swapped out of. 100 sentinel = "force swap, ignore window".
THRESHOLD_STEPS = [50, 75, 90, 100]


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


if __name__ == "__main__":
    cli()
