Force-swap the active Claude account NOW via `cus`. Usage: `/swap [account-name]`. With no argument, the configured strategy picker chooses the best target.

When this skill is invoked:

## Step 1 — orient yourself

Show the user where things stand before acting:

```bash
cus status                # active, per-account state, live sessions
```

Briefly report (1-2 sentences): which account is currently active, what each account's percentages look like, whether any are blocked.

## Step 2 — interpret the user's argument

The user invoked `/swap` with either:
- **No argument** → run `cus auto-swap` (uses configured strategy to pick best target)
- **An account name** → run `cus auto-swap <name>`

Both modes bypass the daemon's threshold check; they swap immediately regardless of current usage %.

The named-argument form takes precedence over the strategy picker. If the user wrote `/swap merkos`, swap to merkos even if it's not the "best" target per the strategy.

If the user wrote something that isn't a valid account name (e.g. `/swap workdir`), tell them — show `cus list` output and ask which one they meant. Don't guess.

## Step 3 — confirm + execute

Before running the swap, briefly tell the user what will happen:
- Which account will become active
- That live sessions keep their loaded tokens until natural refresh (the daemon's hot-swap handles those if hot_swap.enabled)
- New `claude` invocations will use the new account immediately

Then run the swap:

```bash
cus auto-swap [name]
```

Show the output verbatim.

## Step 4 — verify

After the swap:

```bash
cus status                # confirm active = new account
```

Show the new state in 1-2 sentences. Done.

## Important constraints

- **Don't run `cus auto-swap` without showing the user what's about to happen.** The swap affects all subsequent `claude` invocations on this machine.
- **Don't run `cus switch` instead.** That's the older command kept for compatibility; `cus auto-swap` is the preferred force-swap CLI.
- **If the strategy picker returns no target** (all candidates blocked), don't try to force anyway. Tell the user — run `cus sos` for the SOS diagnosis with concrete next steps.

## Quick reference

| Invocation | Maps to |
|---|---|
| `/swap` | `cus auto-swap` (strategy picks best) |
| `/swap merkos` | `cus auto-swap merkos` |
| `/swap 01` | `cus auto-swap 01` |

Done. Stop after Step 4 unless the user asks follow-up.
