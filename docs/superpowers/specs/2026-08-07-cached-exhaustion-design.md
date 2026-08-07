# Honor cached per-model exhaustion until the 7-day window resets

## Problem

An account whose per-model weekly usage is at 100% stays eligible as a swap/launch
target whenever its last poll failed. `_max_model_weekly_from_acct` returns `0.0`
("no cap") for any account where `_pct_is_unknown(acct, "current_7d_pct")` holds —
i.e. `rate_limited`, `token_stale`, `token_expired`, or `poll_error` — so a cached
`Fable=100%` does not exclude anything.

Observed 2026-08-06: `gabai-tefillinconnection-org` and `yaz-dichalane-com` both
carried `Fable=100%~` while remaining admissible on the per-model gate.

The `0.0` is deliberate. It fixes a 2026-07-05 incident where a `token_stale`
account's cached `Fable=100%` was trusted as current and a live Fable session was
moved off an account that actually had headroom. The stated fear is a reading
going stale across a week boundary: a pre-stale 95% that is really ~0 after
rollover.

## Key insight

Usage is monotonically non-decreasing within a window — already encoded in
`_compute_burn_rate` ("a drop means the window reset between polls"). Accounts
store `seven_day_resets_at`.

So while `now < seven_day_resets_at`, a cached reading is a valid **lower bound**:
a cached 100% is still ≥100%. Once that timestamp passes it is genuinely unknown.
The 2026-07-05 incident is exactly the post-rollover case.

This yields the needed asymmetry: a cached reading is safe for **excluding** an
account and unsafe for **admitting** one.

## Scope

In scope — per-model weekly only:

- `_max_model_weekly_from_acct` returns the cached per-model max instead of `0.0`
  when the reading is a valid lower bound **and** that max is ≥100.

Already correct, no change:

- Aggregate 7d. `_account_effective_pct` and `_account_estimated_effective_pct`
  read `current_7d_pct` directly without consulting `_pct_is_unknown`, so a cached
  100.0 already fails the `never_swap_to_pct` hard filter.

Out of scope:

- Swap-away. Already fresh-readings-only; a stale value must not force a live lane
  off an account.
- The aggregate path's mirror bug: post-rollover it keeps excluding on a cached 100
  that may really be ~0. Returning `0.0` there is not the fix — a possibly-full
  account would look empty and *attract* swaps (the stale-low trap that the
  2026-07-07 launch verify-and-repick addresses). A safe fix needs fresh-poll
  verification on the swap-target path.

## Threshold

Exclusion fires at **≥100 only**. A cached 95 stays eligible. This matches the
existing `never_swap_to_pct` default and its recorded rationale: "the sub-100%
gray zone is intentionally left to the scoring logic — below 100% is a judgment
call". Sub-100 cached readings remain too uncertain to strand an account on.

## Design

New predicate beside `_pct_is_unknown`:

```
_cached_7d_floor(acct, now=None) -> float | None
    None   if seven_day_resets_at is missing or unparseable
    None   if now >= seven_day_resets_at        (window rolled; unknown)
    else   acct["current_7d_pct"]               (valid lower bound)
```

`_max_model_weekly_from_acct`, in the branch that currently returns `0.0`:

```
if _cached_7d_floor(acct) is not None:
    cached_max = max of acct["per_model_weekly_pct"].values()
    if cached_max >= 100:
        return cached_max
return 0.0
```

The floor is consulted only in this exclusion path. It never enters scoring, so an
account with a cached low reading still does not look attractive on a stale
number — that half of the 2026-07-05 fix is unchanged.

Routing through `_max_model_weekly_from_acct` means the existing per-model HARD
gate does the work: `pick_swap_target` already returns `None` rather than degrading
onto a model-capped account, giving refuse-and-HOLD behavior with no new control
flow.

## Configuration

None added. `per_model_weekly.gate_enabled` is the existing kill switch.

## Failure modes

All degrade to current behavior (no exclusion): `seven_day_resets_at` missing or
unparseable, `per_model_weekly_pct` missing or malformed, cached max below 100.

## Display

`cus status` renders 7d as `?` for these accounts because there was no
reset-inference for the 7-day window. The floor is that inference, so pre-reset it
can render `100%~` instead. Deferred: it widens the change beyond the decision path
and the `~` convention needs its own review.

## Tests

1. Cached per-model 100, pre-reset → excluded from `pick_swap_target`.
2. Same → excluded from `launch auto` picking.
3. Cached per-model 100, post-reset → NOT excluded (2026-07-05 incident case).
4. Cached per-model 94, pre-reset → NOT excluded.
5. `seven_day_resets_at` missing → NOT excluded.
6. Fresh (non-stale) readings → behavior unchanged.
7. All candidates cached-exhausted → `pick_swap_target` returns `None` (HOLD).
8. Swap-away decisions still ignore the cached value.
