# Committee-loop ledgers — capacity-aware anti-herding spec

Convergence record for the 2026-07-10 committee loop (session `committee-loop-2026-07-10-capacity-aware-anti-herding-20260710-153758-1849881-12712`, CONVERGED at 83042dc).
Spec-level deferred items were folded into the spec post-convergence (rev 5); the items below marked "code" or "cite" are implementation-session material.

## Convergence summary

```
SOUNDNESS-CONVERGED — docs/plans/2026-07-10-capacity-aware-anti-herding.md

Gate mode: soundness (AUTO — document target).

Trigger: clean. Iter-9's completed committee round (quorum 2-of-4: Claude + Codex; both Geminis quota-failed) returned ZERO verified findings of any severity — zero soundness-blocking. Claude's pass independently recomputed every worked formula and example in the spec against cus.py defaults and found all exact; Codex was clean for the fifth consecutive round.

Per-round soundness-blocking applications (trajectory):
- iter-1 (fast mode, Opus): 12 applied (incl. 2 sweep-behavioral) — conversion-table gaps (G9/G10), smart-bonus calibration, plumbing, bootstrap edges
- iter-2 (full committee, Opus): 6 applied incl. 1 CRITICAL (ctx stashed on a discarded shim — structurally unreachable mechanism)
- iter-3 (Opus re-escalation): 2 applied (SOS-probe ctx, global/hybrid ctx)
- iter-4 (Sonnet step-down): 3 applied (rollback path, standalone eager read, invariant scoping)
- iter-5 (Sonnet): 3 applied (auto-swap CLI ctx, diagnostic raw-state, ctx-exception wording)
- iter-6 (Sonnet): 4 applied (reactive-429 ctx, stacked-overshoot risk, loss-reason ctx, fix-#2 scope)
- iter-7 (Sonnet): 2 applied (reactive stash wording + its sweep refinement — both repairs of iter-6's own edit)
- iter-8 (Sonnet): 1 applied (external config-baseline drift: live steps bumped 50→65 by an ops stopgap mid-loop)
- iter-9 (Sonnet): 0 — CLEAN

Panel attrition: Kiro dropped after iter-3 (3× ServiceQuotaExceededException). Gemini and Gemini-Pro dropped after iter-9 (3× agy "Individual quota reached" each, shared account pool, resets ~3h from iter-9). Final panel = Claude + Codex (the 2-reviewer floor).

Polish backlog: 2 entries in .committee-loop-POLISH.md (lane-counting weak form; pool_size "physical cap" wording) — expected and correct under the soundness gate; route to an editorial pass or the implementation session.
Deferred minors: 14 entries in .committee-loop-DEFERRED.md (fixtures, cite drifts, SOS-noise ergonomics, regex edge, intra-cycle unpinned-reference divergence) — implementation-session material.

HOT AREAS (prototype/test these rather than reviewing more prose):
- plumbing / ctx threading: 10 applied findings over 5 iterations — EVERY round through iter-7 found new unreached-ctx call sites (17368 → SOS probes → global/hybrid → auto-swap CLI/diagnostic → reactive-429). Recommendation: before any other implementation work, write the caller-inventory oracle — a grep-driven test asserting every `pick_swap_target`/`decide_swap`/`_launch_candidate_saturated` call site either stashes/threads ctx or is a documented percent-path carve-out. A 20-line test nails deterministically what cost a full committee round per site here.
- bootstrap/validation: 6 applied over 3 iterations — enumerate the tier-sourcing/validation state machine as a table-driven test.
- pct-source rule: 3 applied over 2 iterations — the burn-rate-divergence fixture plus per-site source assertions is the oracle.
- rollout/ops: 3 applied over 3 iterations (rollback procedure, stacked overshoot, live-baseline drift) — the live config drifted DURING this loop; re-verify the baseline annotations (lines 20/24/46) immediately before applying Part A.

The honest bar: SOUNDNESS CLEAN: reviewers stopped finding defects that would make a competent implementer build something incorrect, insecure, or non-functional. This is best-effort adversarial review, NOT a correctness proof. A spec becomes truly verifiable only once it has an executable oracle — when soundness returns diminish (or a hot area persists), the right next step is to IMPLEMENT with the spec's acceptance criteria as the test outline, not to run more review rounds.

End-pass: see final ledger section (this file is written at trigger time; the end-pass runs after it). If the end-pass applied a soundness Critical, the ledger notes it and this convergence is qualified.
```

# Deferred Minor findings

## iter-1
- **claude-M3 (tier source ambiguity):** `_read_access_token_with_expiry` resolves creds through a 3-source freshest-wins chain (cus.py:3202-3216); spec doesn't say which source `_read_rate_limit_tier` parses from, nor cache invalidation on mid-flight tier change. Any source is probably fine — say so.
- **claude-M7 (code, not spec):** `_launch_candidate_saturated` docstring says steps[0] "currently 90" (cus.py:2418); actual is 50. Fix during implementation.
- **codex-min1 (metric not an exact oracle):** rollout's <5% counterfactual metric compares raw per-lane headroom while formula 1 includes bonuses/isolation; useful operationally, not a direct correctness oracle for the ranking.
- **codex-min2 (evidence not reproducible from checkout):** herding_analysis.py reads private absolute-path logs; 896/79/62% figures can't be independently re-derived from the repo. Consider committing a redacted fixture.
- **codex-min3 (success criterion coupling):** "no 429-halt while ≥1 ref-unit per-lane headroom exists" can be violated by poll overshoot before out-of-scope fixes #1/#2 land; consider making it directional or moving to the combined rollout.

## iter-2
- **gemini-min1 (G5 suppression log stays percent):** cus.py:7385-7393 builds the min_improvement_gate suppression message from `target_eff`/`active_eff` percents; the G5 row converts only the hold decision, so gate-on diagnostics print percent numbers that didn't produce the hold. Fix during implementation (log the units comparison when gate-on).
- **gemini-min2 (17368 ctx-missing fallback):** plan didn't specify behavior for callers reaching the verify loop without launch ctx. Largely superseded by 2-geminipro-ctx-lifecycle (17368 now builds ctx fresh at the call site); residual: direct/test invocations of `_launch_candidate_saturated` default to percent path (ctx=None), which is the documented sentinel.
- **geminipro-min1 (invalid reference_x fallback):** line 65 validates `reference_x ≥ 1` but doesn't state runtime fallback if present-but-invalid (0/negative/non-numeric; it's a divisor). Say: treat as absent (snapshot path) or fall to 1 with SOS.
- **geminipro-min2 (absent reference_x re-snapshots each cycle):** the absent-value bootstrap recomputes the observed fleet minimum every cycle until pinned — a transient re-instance of the dynamic-baseline instability pinning was meant to solve. Consider persisting the first snapshot until pinned.
- **geminipro-min3 (noisy drift warning on all-unknown fleet):** with no parsed/overridden tiers, observed minimum (1) vs pinned reference (e.g. 5) fires the retune warning every cycle despite being behavior-neutral (all ratios 1). Suppress in the all-unknown case.

## iter-3
- **claude-min1 (G8 source-side sentinel clamp unspecified):** the two G8 source-side probe sites read the RAW `next_swap_at_pct` (10266, 14887 — no ≥100 clamp), while G4 clamps; at the ladder-run-off sentinel (100) a gate-on report would say "doesn't want to rotate" while G4 trips at the clamped steps[-1] line, breaking the G8 row's "reports match gate-on leave decisions" claim in that edge. Fix during implementation: probes use the same clamped local as G4 (line 68's discipline generalizes).
- **claude-min2 (citation drift):** `_try`'s `shim = dict(state)` is at cus.py:2366 (spec cites 2372, off by 6); 7931 is an inline `_lane_load` build on the reactive-429 escape path, not a `_cur_lane_load()` call (real call sites: 7656, 7748). Pure cite fixes; grep-recoverable by any implementer.

## iter-4
- **codex-min1 (intra-cycle unpinned-reference divergence):** with reference_x unpinned, each caller's independent `_capacity_ctx` build over a (possibly account-filtered) shim can observe a different fleet minimum within one cycle. Narrower sibling of the deferred cross-cycle re-snapshot item; both vanish once reference_x is pinned (the spec's recommended state). Implementation note: resolve the observed minimum once per cycle over the UNFILTERED fleet and pass it to all builders.
- **gemini-min1 (regex underscore prefix):** `_(\d+)x(?=[_-]|$)` requires a leading underscore; bare `20x` or `tier-20x` fall to neutral. Safe fallback, silent. Suggested generalization: `(?:[_-]|^)(\d+)x(?=[_-]|$)`.
- **gemini-min2 (retune warning on deliberate pins):** the stateless drift warning fires every cycle whenever observed-min ≠ pinned reference, including deliberate pins (e.g. smallest accounts temporarily disabled). Consider warning only when observed-min < pinned reference.

## iter-5
- **geminipro-min1 (observed-minimum domain):** line 65 never states the bootstrap "observed fleet minimum" is computed only over accounts with a parsed/overridden tier (unknown-tier accounts' capacity_x IS reference_x — circular if included). All-unknown edge already handled (snapshot 1); state the general-case domain explicitly during implementation.
- **geminipro-min2 (sub-reference warning vs disabled accounts):** line 66 lists "disabling the account" as a remedy but the per-cycle warning as written would keep firing for it. Codebase precedent: cus.py:10680-10693 silences per-account SOS for operator-disabled accounts (soft INFO line). Apply the same treatment.

## iter-8
- **claude-min1 (cite drift 4876→4877):** the burn-soon bonus "+~98" comment is at cus.py:4877, spec line 97 cites 4876. Same class as the deferred 2366/2372 drift; fix cites in one pass during implementation.

## iter-6
- **claude-min1 (G7 fixture gap):** G7 (drain/strict_priority) is the only converted gate with no named fixture in the Rollout list (the heterogeneous bullet is scoped to smart/lowest_usage). Latent (operator config uses smart), but add a drain ordering + accept fixture during implementation.

---

# Polish backlog (soundness gate — never applied by the loop, never blocks convergence)

## 1-codex-lane-counting (iter-1, Codex C2 sub-claim, verifier-confirmed weak form)
- **Claim:** `lanes` built from `occupied_slot_accounts(state)` counts slots/mounts; lane-shared sessions on one slot count once, so repeated `--join` launches never increase the `÷(lanes+1)` divisor.
- **Evidence:** cus.py:7431-7453 (slot lists), 17408-17433 (join mints no slot), 7607/7613 (swap path also counts slots — spec is CONSISTENT with existing `_lane_load` semantics; the "undercounts vs swap path" strong form was REFUTED).
- **Proposed polish:** add an accepted-limitation sentence at spec line ~91: multi-session lanes undercount burn pressure; they share one login family and swap together; rising pct self-corrects within a poll cycle.

## 1-codex-poolsize-cap (iter-1, Codex I5, verifier-confirmed)
- **Claim:** Risks bullet says stacking is "bounded by ... the physical `pool_size: 4` family cap" — false: pool_size is an onboarding/SOS target (default 3), not a hard cap; N provisioned families ⇒ N+1 concurrent mounts; lane-shared sessions are not bounded by family count at all.
- **Evidence:** cus.py:317-326 ("pool_size does NOT hard-cap families"), docs/RUNBOOK.md ~240, config pool_size: 4.
- **Proposed polish:** reword the Max-account-stacking risk bullet to cite provisioned-family count (N⇒N+1 mounts) and note lane-shared sessions are unbounded by families; the health line's per-lane form remains the operative bound.
