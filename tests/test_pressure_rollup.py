"""Task 13 (spec-2 token-pressure forecaster, STAGE 1, Phase D): coordinator
cwd-prefix rollup -- rolls a child session's burn (subagents/workflows a
coordinator spawned) up to the coordinator for §5.2 classification +
targeting, while keeping Task 11's per-(account, session) burn attribution
exactly as-is (a rollup is a VIEW, never a re-attribution).

Covers ``_registered_coordinators``, ``_coordinator_of``, ``_rollup_children``
-- the three functions this task introduces.

Pinned FACT #10 coordinator layouts (verbatim, committee #9):
  - ``<repo>/.claude/worktrees/<name>``
  - ``<repo>/.worktrees/<name>``
  - transcript child nesting: ``<slug>/<parent-sessionId>/subagents/...`` and
    ``<slug>/<parent-sessionId>/workflows/...``
Rule: "longest registered-coordinator cwd prefix" wins, matched on path
SEGMENT boundaries (never a raw string prefix -- ``/repo/.worktrees/foo``
must NOT match ``/repo/.worktrees/foobar``). An unrecognized cwd -> NO
rollup (``_coordinator_of`` returns ``None``) + low_confidence downstream.

LOAD-BEARING invariant (§1): each unit of burn is counted exactly once -- a
child rolled up into its coordinator is DROPPED from the candidate set, or
its burn is double-counted (coordinator + child) -> under-throttle.

HARNESS (reused by every ``tests/test_pressure_*.py``): cus.py is a single
PEP-723 ``uv run --script`` file whose deps (click, pyyaml) plus dev-only
pytest are importable in the test env; the file imports as a plain module.
Add the repo root to ``sys.path`` and ``import cus``. Run with ``python -m
pytest tests/ -q`` (same command CI uses); a single file runs standalone via
``python tests/test_pressure_rollup.py``.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _row(session_id: str, cwd: str, account: str = "acctX") -> dict:
    """A `_parse_sessions_log()`-shaped launch row (FACT #5, 6-col) -- only
    the fields `_registered_coordinators` consumes are filled in; the rest
    mirror `tests/test_pressure_attribution.py`'s `_row` helper shape."""
    return {
        "ts": "2026-07-14T12:00:00Z",
        "session_id": session_id,
        "account": account,
        "pane": "%1",
        "tmux_socket": "/tmp/tmux-1000/default",
        "cwd": cwd,
    }


def _make_nested_transcript(projects_dir: Path, slug: str, parent_session_id: str,
                             kind: str, filename: str) -> None:
    """Materializes an on-disk `<slug>/<parent-sessionId>/<kind>/<filename>`
    transcript file (FACT #10's transcript-nesting layout) -- `kind` is
    `"subagents"` or `"workflows"`, matching `_pressure_transcript_paths`'s
    own glob convention (Task 10, FACT #6)."""
    d = projects_dir / slug / parent_session_id / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text('{"type": "assistant"}\n')


# ---------------------------------------------------------------------------
# _registered_coordinators: PRODUCER of the coordinator-cwd set (finding 14)
# ---------------------------------------------------------------------------

def test_registered_set_built_from_sessions_and_layouts(tmp_path):
    """A launch-row cwd matching a FACT #10 layout IS registered; a
    launch-row cwd with no layout match and no nested children under it is
    NOT -- pattern-detected from the pinned layouts, never an external
    registry (finding 14)."""
    rows = [
        _row("sCoord", "/home/y/repo/.worktrees/agentA"),
        _row("sPlain", "/home/y/repo/plain-project"),  # no layout, no children
    ]
    registered = cus._registered_coordinators(rows, tmp_path)

    assert registered == {"/home/y/repo/.worktrees/agentA"}
    assert "/home/y/repo/plain-project" not in registered


def test_worktree_layouts_recognized(tmp_path):
    """Both pinned FACT #10 worktree layouts register: the dotted
    `<repo>/.worktrees/<name>` AND the `.claude`-nested
    `<repo>/.claude/worktrees/<name>`."""
    rows = [
        _row("sDot", "/home/y/repo/.worktrees/agentA"),
        _row("sClaude", "/home/y/repo/.claude/worktrees/agentB"),
    ]
    registered = cus._registered_coordinators(rows, tmp_path)

    assert "/home/y/repo/.worktrees/agentA" in registered
    assert "/home/y/repo/.claude/worktrees/agentB" in registered


def test_bare_worktrees_not_coordinator(tmp_path):
    """FACT #10 pins exactly two coordinator layouts: `.claude`-nested
    `<repo>/.claude/worktrees/<name>` and dotted `<repo>/.worktrees/<name>`.
    A bare `worktrees` segment with no `.claude` ancestor (a plain `git
    worktree` convention, `<repo>/worktrees/<name>`) is NOT a Claude
    coordinator layout -- registering it would fold a genuine burner
    session nesting under that cwd into a false "coordinator" and drop it
    from the §5.2 targeting candidate set (wrong-session targeting)."""
    rows = [
        _row("sBare", "/home/y/repo/worktrees/agentA"),  # bare, no .claude
        _row("sDot", "/home/y/repo/.worktrees/agentB"),
        _row("sClaude", "/home/y/repo/.claude/worktrees/agentC"),
    ]
    registered = cus._registered_coordinators(rows, tmp_path)

    assert "/home/y/repo/worktrees/agentA" not in registered
    assert "/home/y/repo/.worktrees/agentB" in registered
    assert "/home/y/repo/.claude/worktrees/agentC" in registered


def test_transcript_subagent_nesting(tmp_path):
    """A launch-row cwd that does NOT match a worktree layout is still
    registered as a coordinator if a child transcript nests under its OWN
    session dir -- `<slug>/<parent-sessionId>/subagents/agent-*.jsonl` OR
    `.../workflows/*.jsonl` (FACT #10's alternate condition)."""
    _make_nested_transcript(tmp_path, "myproj-slug", "sSubagentParent",
                             "subagents", "agent-1.jsonl")
    _make_nested_transcript(tmp_path, "myproj-slug", "sWorkflowParent",
                             "workflows", "wf-1.jsonl")

    rows = [
        _row("sSubagentParent", "/home/y/plainrepo"),
        _row("sWorkflowParent", "/home/y/otherrepo"),
        _row("sNoChildren", "/home/y/thirdrepo"),  # no nesting -> not a coordinator
    ]
    registered = cus._registered_coordinators(rows, tmp_path)

    assert "/home/y/plainrepo" in registered
    assert "/home/y/otherrepo" in registered
    assert "/home/y/thirdrepo" not in registered


# ---------------------------------------------------------------------------
# _coordinator_of: longest registered-coordinator cwd PREFIX (segment-wise)
# ---------------------------------------------------------------------------

def test_longest_prefix_wins():
    """When a child cwd sits under TWO registered coordinators (nested
    worktrees), the LONGEST (deepest) registered prefix wins -- and a
    partial path-SEGMENT match (`/repo/.worktrees/foo` vs
    `/repo/.worktrees/foobar`) is never mistaken for a real nesting."""
    registered = {
        "/repo/.worktrees/agent1",
        "/repo/.worktrees/agent1/nested",
    }

    deep_child = "/repo/.worktrees/agent1/nested/sub"
    assert cus._coordinator_of(deep_child, registered) == "/repo/.worktrees/agent1/nested"

    shallow_child = "/repo/.worktrees/agent1/other"
    assert cus._coordinator_of(shallow_child, registered) == "/repo/.worktrees/agent1"

    # Partial-segment false match guard: "foobar" is a DIFFERENT segment
    # from "foo", not a path nested under it -- raw string prefix would
    # wrongly match this; segment-wise matching must not.
    registered_foo = {"/repo/.worktrees/foo"}
    assert cus._coordinator_of("/repo/.worktrees/foobar", registered_foo) is None
    assert cus._coordinator_of("/repo/.worktrees/foo/child", registered_foo) == \
        "/repo/.worktrees/foo"


def test_unrecognized_no_rollup_low_confidence():
    """A cwd that matches NO registered coordinator prefix at all resolves
    to `None` -- the "no rollup" signal; a caller attaches low_confidence
    downstream (this function itself doesn't compute that flag, per the
    brief's interface note). Feeding such a session into `_rollup_children`
    (absent from `coord_map`) leaves it exactly as its own, un-rolled-up
    candidate."""
    registered = {"/repo/.worktrees/agent1"}
    assert cus._coordinator_of("/totally/unrelated/cwd", registered) is None
    assert cus._coordinator_of(None, registered) is None
    assert cus._coordinator_of("/repo/.worktrees/agent1", set()) is None

    table = cus.AttributionTable()
    table.per_session[("acctX", "sLoner")] = 6.0
    rolled = cus._rollup_children(table, coord_map={})  # sLoner has no coordinator

    assert rolled.per_session[("acctX", "sLoner")] == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# _rollup_children: burn summed to the coordinator, children DROPPED once
# ---------------------------------------------------------------------------

def test_children_excluded_from_walk():
    """Child burn sums INTO the coordinator's bucket, and the child's own
    (account, session_id) key is DROPPED from the returned table entirely --
    the §5.2 candidate walk over `.per_session` never sees it again, so the
    same unit of burn is never double-counted (once as itself, once inside
    its coordinator)."""
    table = cus.AttributionTable()
    table.per_session[("acctA", "coord")] = 10.0
    table.per_session[("acctA", "child1")] = 5.0
    table.per_session[("acctB", "child2")] = 3.0  # no coordinator -> untouched
    table.session_pane["coord"] = "%1"
    table.session_pane["child1"] = "%2"

    coord_map = {"child1": "coord"}  # child2 intentionally absent
    rolled = cus._rollup_children(table, coord_map)

    # Rolled up: coordinator's bucket now carries its own + child1's burn.
    assert rolled.per_session[("acctA", "coord")] == pytest.approx(15.0)
    # Counted once: child1 is GONE as its own candidate.
    assert ("acctA", "child1") not in rolled.per_session
    # Untouched: child2 has no registered coordinator, stays its own candidate.
    assert rolled.per_session[("acctB", "child2")] == pytest.approx(3.0)

    candidate_session_ids = {sid for (_acct, sid) in rolled.per_session}
    assert "child1" not in candidate_session_ids
    assert "coord" in candidate_session_ids
    assert "child2" in candidate_session_ids

    # No unit of burn lost OR duplicated across the regroup.
    assert sum(rolled.per_session.values()) == pytest.approx(
        sum(table.per_session.values()))

    # session_pane stays internally consistent: child1's pane entry must
    # NOT survive once child1's own (account, session_id) key is dropped
    # from per_session -- a stale pane entry for a session_id absent from
    # per_session would be an inconsistent view.
    assert "child1" not in rolled.session_pane
    assert rolled.session_pane["coord"] == "%1"


def test_per_account_attribution_preserved():
    """Rollup is a classification/targeting VIEW: it must never mutate the
    Task 11 `AttributionTable` it was given, and it must never re-attribute
    a child's burn across accounts -- a child still belongs to whatever
    account it was attributed to, even once grouped under its coordinator's
    session bucket for targeting."""
    table = cus.AttributionTable()
    table.per_session[("acctCoord", "coord")] = 8.0
    table.per_session[("acctChild", "child3")] = 4.0  # DIFFERENT account
    before = dict(table.per_session)

    rolled = cus._rollup_children(table, coord_map={"child3": "coord"})

    # The input table is untouched -- Task 11's per-account attribution
    # stays exactly as it was, byte-for-byte.
    assert table.per_session == before
    assert table.per_session[("acctChild", "child3")] == pytest.approx(4.0)
    assert rolled is not table

    # Grouped under the coordinator's SESSION for targeting, but the burn
    # keeps its OWN account -- never re-attributed to the coordinator's
    # account.
    assert rolled.per_session[("acctChild", "coord")] == pytest.approx(4.0)
    assert rolled.per_session[("acctCoord", "coord")] == pytest.approx(8.0)
    assert ("acctChild", "child3") not in rolled.per_session


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
