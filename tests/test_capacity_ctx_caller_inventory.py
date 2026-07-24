"""Caller-inventory oracle for the capacity-aware anti-herding rollout (Phase 0a).

Why this exists (design-review loop, 2026-07-10 spec): a review found ctx-
threading call sites kept being missed one round at a time — 10 findings over
5 rounds, because there was no ground truth for "every call site that needs a
capacity-context decision." This test IS that ground truth: it greps cus.py
for every call site of the four swap-decision entry points and requires each
one be explicitly classified in EXPECTED, so a newly added/removed site fails
loudly instead of silently slipping through review.

Classification values (see spec Rollout §2 bullet 1):
  "ctx"      — site threads/stashes capacity ctx (converted).
  "carveout" — documented percent-path carve-out (deliberately NOT ctx-aware).
  "pending"  — not yet converted (today: ALL sites, since cus.py is unmodified
               and CAPACITY_AWARE_PLUMBING_COMPLETE does not exist yet).

Comment convention for EXPECTED: the trailing "# L<line>" on each entry is the
call site's line in cus.py AS OF THIS COMMIT — advisory only (it drifts as
cus.py changes; the fingerprint key, not the line number, is authoritative).
When a later task converts a site, change its value to "ctx"/"carveout" and
extend the comment with the task that did it, e.g.:
    ("foo", "decide_swap", 1): "ctx",  # L1234 task-4: threads capacity budget

Run standalone:  python3 -m pytest tests/test_capacity_ctx_caller_inventory.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402

# The four swap-decision entry points whose call sites must all be classified.
CALLEE_NAMES = (
    "pick_swap_target",
    "decide_swap",
    "_target_would_immediately_re_trip",
    "_launch_candidate_saturated",
)

Fingerprint = tuple[str, str, int]  # (enclosing_def_name, callee_name, count)

_DEF_RE = re.compile(r"^(\s*)(?:async\s+def|def)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')
_TRIPLE_RE = re.compile(r'"""|\'\'\'')


def _code_only(line: str, was_in_docstring: bool) -> str:
    """The substring(s) of `line` that fall OUTSIDE any triple-quoted
    docstring span (handling a line that opens/closes/both on the same
    physical line), concatenated in order.

    Needed because docstring PROSE routinely contains bare parens/brackets
    and `#` (e.g. an issue reference like "(GH #79)") that must never feed
    the bracket-depth / comment-stripping below — cus.py's very first
    multi-line docstring does exactly this and will silently wreck a naive
    tracker.
    """
    parts = []
    pos = 0
    in_ds = was_in_docstring
    for m in _TRIPLE_RE.finditer(line):
        if not in_ds:
            parts.append(line[pos:m.start()])
        pos = m.end()
        in_ds = not in_ds
    if not in_ds:
        parts.append(line[pos:])
    return "".join(parts)


def discover_call_sites(source: str) -> dict[Fingerprint, int]:
    """Scan `source` (cus.py's text) line-by-line for call sites of
    CALLEE_NAMES and return {fingerprint: line_no (1-based)}.

    A call site is a line containing "<name>(" that is not the "def <name>("
    line itself, not a comment-only line, and not inside a docstring (simple
    triple-quote toggle — the four names are distinctive enough that this
    heuristic has near-zero false positives).

    Each site's enclosing function is resolved via an indentation-based scope
    stack (not just "nearest preceding def at lower indent" scanned blindly —
    that naive version mislabels a call that comes after a NESTED helper
    def's own body has already ended, attributing it to the helper instead of
    the true outer function; the stack is popped back to the right level
    each time indentation returns to it). A running bracket-depth counter
    (with string/comment/docstring content excluded) protects that stack from
    multi-line statements — e.g. a def signature whose closing ") -> X:" line
    is flush with the "def" line's own indent must not be mistaken for the
    end of that def's body before the body has even started.
    """
    lines = source.splitlines()

    in_docstring = False
    bracket_depth = 0
    stack: list[tuple[int, str]] = []  # [(indent, enclosing_name), ...]
    enc = "<module>"
    counts: dict[tuple[str, str], int] = {}
    sites: dict[Fingerprint, int] = {}

    for i, line in enumerate(lines):
        stripped = line.strip()

        was_in_docstring = in_docstring
        triple_count = line.count('"""') + line.count("'''")
        if triple_count % 2 == 1:
            in_docstring = not in_docstring

        if not stripped:
            continue  # blank lines affect neither scope nor detection

        def_m = None
        is_new_logical_line = bracket_depth == 0 and not was_in_docstring
        if is_new_logical_line:
            indent = len(line) - len(line.lstrip())
            while stack and stack[-1][0] >= indent:
                stack.pop()
            enc = stack[-1][1] if stack else "<module>"
            def_m = _DEF_RE.match(line)
            if def_m:
                stack.append((indent, def_m.group(2)))

        code_part = _code_only(line, was_in_docstring)
        clean = _STRING_RE.sub("", code_part)
        hash_idx = clean.find("#")
        if hash_idx != -1:
            clean = clean[:hash_idx]  # drop a trailing comment before counting brackets
        bracket_depth += clean.count("(") + clean.count("[") + clean.count("{")
        bracket_depth -= clean.count(")") + clean.count("]") + clean.count("}")
        bracket_depth = max(bracket_depth, 0)  # safety net vs. a stray closer

        if was_in_docstring:
            continue  # whole line was inside a docstring: skip call detection
        if stripped.startswith("#"):
            continue

        for name in CALLEE_NAMES:
            if name + "(" not in line:
                continue
            if def_m and def_m.group(2) == name:
                continue  # the "def <name>(" line itself
            key = (enc, name)
            counts[key] = counts.get(key, 0) + 1
            sites[(enc, name, counts[key])] = i + 1

    return sites


# One entry per discovered call site, sorted by (enclosing_def_name,
# callee_name, count). Populated from the actual inventory below (verified by
# hand against `grep -n` on cus.py); all "pending" today since none of these
# sites have been converted yet.
EXPECTED: dict[Fingerprint, str] = {
    # Task 5 (capacity-aware spec 2026-07-10, Phase 2b) converted every swap-
    # decision call site to thread or stash a capacity ctx; there are no
    # percent-path carve-outs among these four callees (the documented carve-outs
    # — `cus switch` single-account validation, per-model weekly gates,
    # never_swap_to_pct, and bbr min_candidate_headroom_pct — are gates that are
    # NOT call sites of pick_swap_target / decide_swap /
    # _target_would_immediately_re_trip / _launch_candidate_saturated, so they
    # never appear in this inventory).
    ("_candidate_is_valid_premium_target", "pick_swap_target", 1): "ctx",  # L10692 task-5: SOS probe stashes fleet ctx onto its 2-acct shim (G8/formula 2)
    ("_hybrid_cycle", "decide_swap", 1): "ctx",  # L9099 task-5: shared-mount decide shim carries stashed ctx
    ("_launch_candidate_saturated", "_target_would_immediately_re_trip", 1): "ctx",  # L2483 task-5: launch-accept wall threads name+ctx (G3/formula 2)
    ("_launch_prepare", "_launch_candidate_saturated", 1): "ctx",  # L18419 task-5: verify-and-repick builds fresh ctx, passes name+ctx
    ("_maybe_burn_before_reset", "pick_swap_target", 1): "ctx",  # L7518 task-5: bbr target pick is ctx-aware via stash-or-fresh-build fallback
    ("_premium_target_loss_reason", "_target_would_immediately_re_trip", 1): "ctx",  # L10665 task-5: loss labeller gains ctx param, threads name+ctx (G8/G2)
    ("_try", "pick_swap_target", 1): "ctx",  # L2389 task-5: pick_launch_account shim builder stashes ctx (nested in pick_launch_account)
    ("auto_swap_cmd", "pick_swap_target", 1): "ctx",  # L15085 task-5: auto-swap CLI stashes ctx on a local shim (G8)
    ("check_rate_limit_reactive", "_target_would_immediately_re_trip", 1): "ctx",  # L13687 task-5: global reactive veto threads name+ctx
    ("check_rate_limit_reactive", "pick_swap_target", 1): "ctx",  # L13674 task-5: global reactive escape stashes ctx on a local shim
    ("check_rate_limit_reactive_per_session", "_target_would_immediately_re_trip", 1): "ctx",  # L8698 task-5: post-merge unsafe-target veto threads name+ctx
    ("check_rate_limit_reactive_per_session", "pick_swap_target", 1): "ctx",  # L8688 task-5: per_session reactive shim carries claim-aware stashed ctx
    ("decide_slot_swaps", "_target_would_immediately_re_trip", 1): "ctx",  # L8463 task-4/5: fan-out re-pick health check threads name+shim2 ctx (G2)
    ("decide_slot_swaps", "decide_swap", 1): "ctx",  # L8369 task-5: per-group shim carries claim-aware stashed ctx
    ("decide_slot_swaps", "decide_swap", 2): "ctx",  # L8383 task-5: std-pool degrade retry reuses same ctx-stashed shim
    ("decide_slot_swaps", "pick_swap_target", 1): "ctx",  # L8461 task-5: fan-out re-pick shim2 carries fresh claim-aware stashed ctx
    # decide_swap's four picks, in textual order (counts are occurrence-order;
    # descriptions realigned 2026-07-24 when the #188 port added a site ahead
    # of the other three): 1 = Trigger 0 disabled-account eviction, 2 =
    # persisted per-model hard-cap on no-fresh-poll, 3 = hard 7d cap trip,
    # 4 = progressive ladder. All thread ctx the same way — via the caller's
    # stashed state + the picker's self-build fallback.
    ("decide_swap", "pick_swap_target", 1): "ctx",  # upstream #188 port 2026-07-24: Trigger 0 disabled-evict pick
    ("decide_swap", "pick_swap_target", 2): "ctx",  # per-model port 2026-07-05: persisted per-model hard-cap force-swap on no-fresh-poll
    ("decide_swap", "pick_swap_target", 3): "ctx",  # task-5: hard-cap trip pick
    ("decide_swap", "pick_swap_target", 4): "ctx",  # task-5: progressive-ladder pick
    ("diagnose", "pick_swap_target", 1): "ctx",  # L11159 task-5: SOS Condition 2b shim carries stashed ctx (G8/formula 2)
    ("diagnose", "pick_swap_target", 2): "ctx",  # L11173 task-5: premium-degrade probe reuses same ctx-stashed shim
    ("one_cycle", "decide_swap", 1): "ctx",  # L15844 task-5: decide_state shim carries stashed ctx
    ("one_cycle", "pick_swap_target", 1): "ctx",  # L15887 task-5: no-target diagnostic re-picks on the ctx-stashed decide_state
    ("pick_swap_target", "_target_would_immediately_re_trip", 1): "ctx",  # L5194 task-5: reserve-preference self-call threads name+ctx (self-build ctx)
    ("pick_swap_target", "_target_would_immediately_re_trip", 2): "ctx",  # L5253 task-5: with_headroom filter self-call threads name+ctx (self-build ctx)
}


def test_all_call_sites_are_classified():
    """Test A (always on): the discovered call-site inventory must exactly
    match EXPECTED's keys. A mismatch means cus.py grew or lost a call site
    of one of the four capacity-decision entry points since EXPECTED was last
    updated — classify it ("ctx"/"carveout"/"pending") and add/remove it from
    EXPECTED rather than silently letting it slip past review."""
    discovered = discover_call_sites(Path(cus.__file__).read_text())
    discovered_keys = set(discovered)
    expected_keys = set(EXPECTED)

    new = discovered_keys - expected_keys
    removed = expected_keys - discovered_keys
    assert discovered_keys == expected_keys, (
        "cus.py's call-site inventory for "
        f"{', '.join(CALLEE_NAMES)} changed — update EXPECTED in "
        f"{Path(__file__).name} and classify each site as "
        '"ctx" (threads/stashes capacity ctx), "carveout" (documented '
        'percent-path carve-out), or "pending" (not yet converted).\n'
        f"NEW sites (add to EXPECTED): "
        f"{sorted((fp, discovered[fp]) for fp in new)}\n"
        f"REMOVED sites (delete from EXPECTED): {sorted(removed)}"
    )


def test_no_pending_once_plumbing_complete():
    """Test B: once a later task flips cus.CAPACITY_AWARE_PLUMBING_COMPLETE
    to True, every site in EXPECTED must have been converted off "pending".
    Today that constant doesn't exist in cus.py, so this is a no-op pass."""
    if not getattr(cus, "CAPACITY_AWARE_PLUMBING_COMPLETE", False):
        return
    pending = sorted(fp for fp, cls in EXPECTED.items() if cls == "pending")
    assert not pending, (
        "cus.CAPACITY_AWARE_PLUMBING_COMPLETE is True but sites in "
        f"EXPECTED are still 'pending': {pending}"
    )
