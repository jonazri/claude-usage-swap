#!/usr/bin/env python3
"""Herding analysis over ~/claude-accounts/decisions.jsonl + 429.log.

Questions:
1. How prevalent is stacking (>=2 lanes on one account)?
2. Burn rate vs lane-count: does stacking burn accounts proportionally faster?
3. Counterfactual: at each pool-double-book swap, would a higher target-health
   line (steps[0] = 60/70/80 instead of 50) have offered a distinct healthy
   target so fan-out could spread instead of stack?
4. Do 429s cluster in stacked periods?
"""
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

DIR = "/home/yaz/claude-accounts"

def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

recs = [json.loads(l) for l in open(f"{DIR}/decisions.jsonl") if l.strip()]
recs.sort(key=lambda r: r["when"])

# ---- 1. occupancy timeline from hold reasons + swap records -------------
OCC_RE = re.compile(r"occupied: (.+)$")
GRP_RE = re.compile(r"([\w.-]+)\((slot-[\d+a-z-]+)\)")

occ_samples = []  # (dt, {account: n_lanes})
for r in recs:
    if r.get("gate") != "no_slot_moves":
        continue
    m = OCC_RE.search(r.get("reason") or "")
    if not m:
        continue
    occ = {}
    for acct, slots in GRP_RE.findall(m.group(1)):
        occ[acct] = len(slots.split("+"))
    if occ:
        occ_samples.append((ts(r["when"]), occ))

total = len(occ_samples)
stacked2 = sum(1 for _, o in occ_samples if max(o.values()) >= 2)
stacked3 = sum(1 for _, o in occ_samples if max(o.values()) >= 3)
stacked4 = sum(1 for _, o in occ_samples if max(o.values()) >= 4)
lanes_total = [sum(o.values()) for _, o in occ_samples]
print(f"== occupancy samples (hold cycles): {total} over "
      f"{occ_samples[0][0]:%m-%d %H:%M} .. {occ_samples[-1][0]:%m-%d %H:%M} UTC")
print(f"   lanes live: median {statistics.median(lanes_total):.0f}, max {max(lanes_total)}")
print(f"   cycles with max-stack>=2: {stacked2} ({100*stacked2/total:.0f}%)  "
      f">=3: {stacked3} ({100*stacked3/total:.0f}%)  >=4: {stacked4} ({100*stacked4/total:.0f}%)")

# ---- 2. burn rate vs lane count -----------------------------------------
# Join consecutive occupancy samples <=15 min apart; for each account present
# in both with rising 5h pct (no reset between), attribute delta to its lane
# count. Accounts snapshot lives in every record.
burn_by_lanes = defaultdict(list)  # lanes -> %/hour samples
prev = None
for r in recs:
    if r.get("gate") != "no_slot_moves":
        continue
    m = OCC_RE.search(r.get("reason") or "")
    if not m or not r.get("accounts"):
        continue
    occ = {a: len(s.split("+")) for a, s in GRP_RE.findall(m.group(1))}
    cur = (ts(r["when"]), occ, r["accounts"])
    if prev:
        dt_h = (cur[0] - prev[0]).total_seconds() / 3600
        if 0 < dt_h <= 0.25:
            for acct, snap in cur[2].items():
                if acct not in prev[2]:
                    continue
                d5 = snap.get("5h", 0) - prev[2][acct].get("5h", 0)
                if d5 <= 0:   # reset or idle
                    continue
                lanes = cur[1].get(acct, 0)
                burn_by_lanes[lanes].append(d5 / dt_h)
    prev = cur

print("\n== 5h burn rate (%/hour) by lane count (positive-delta samples)")
for lanes in sorted(burn_by_lanes):
    v = burn_by_lanes[lanes]
    print(f"   {lanes} lane(s): n={len(v):4d}  median {statistics.median(v):5.1f}  "
          f"p90 {statistics.quantiles(v, n=10)[8] if len(v)>=10 else max(v):5.1f}")

# ---- 3. counterfactual health line at stacking swaps ---------------------
# For each swap onto an account that already backs >=1 lane (double-book /
# stacking move), check alternatives in the same snapshot.
def effective(a):
    return max(a.get("5h", 0), a.get("7d", 0))

# occupancy at swap time: nearest earlier occupancy sample, updated by swaps since
def occ_at(t):
    o = {}
    for st, so in occ_samples:
        if st <= t:
            o = dict(so)
        else:
            break
    return o

lines = [50, 60, 70, 80]
stack_swaps = 0
rescued = Counter()
alt_detail = []
for r in recs:
    if r.get("action") != "swap":
        continue
    tgt = r.get("target")
    accounts = r.get("accounts") or {}
    if not tgt or tgt not in accounts:
        continue
    reason = r.get("reason") or ""
    if "double-book" not in reason:
        continue
    stack_swaps += 1
    when = ts(r["when"])
    o = occ_at(when)
    for line in lines:
        alts = [n for n, a in accounts.items()
                if n != tgt and n != (r.get("where") or {}).get("from")
                and n != "default"
                and effective(a) < line and a.get("5h", 100) < 95
                and o.get(n, 0) == 0]
        if alts:
            rescued[line] += 1
            if line == 70:
                alt_detail.append((r["when"][:16], tgt, effective(accounts[tgt]),
                                   {n: effective(accounts[n]) for n in alts}))

print(f"\n== stacking (pool double-book) swaps: {stack_swaps}")
for line in lines:
    print(f"   health line {line}%: {rescued[line]}/{stack_swaps} had >=1 unoccupied "
          f"distinct target below line ({100*rescued[line]/max(1,stack_swaps):.0f}% rescuable)")
print("\n   sample rescues at line=70 (when, chosen_tgt@eff, alternatives):")
for row in alt_detail[:8]:
    print("   ", row)

# ---- 4. 429s vs stacking --------------------------------------------------
events = []
for l in open(f"{DIR}/429.log"):
    parts = l.strip().split(",")
    if len(parts) >= 4 and parts[0].startswith("2026"):
        events.append(ts(parts[0]))
in_stack = 0
for e in events:
    o = occ_at(e)
    if o and max(o.values()) >= 2:
        in_stack += 1
print(f"\n== 429 hook events: {len(events)}; during max-stack>=2 occupancy: "
      f"{in_stack} ({100*in_stack/max(1,len(events)):.0f}%)")

# ---- 5. overnight window replay ------------------------------------------
print("\n== overnight 2026-07-09T20:00Z .. 07-10T08:00Z, effective pct per account per hour")
names = sorted({n for r in recs for n in (r.get('accounts') or {})})
print("   time  " + "  ".join(n.replace('yaz-','').replace('tefillinconnection','tfc').replace('-org','').replace('-com','')[:12].rjust(12) for n in names))
last_hr = None
for r in recs:
    t = ts(r["when"])
    if not (datetime(2026,7,9,20,tzinfo=timezone.utc) <= t <= datetime(2026,7,10,8,tzinfo=timezone.utc)):
        continue
    if not r.get("accounts"):
        continue
    hr = t.strftime("%d %H")
    if hr == last_hr:
        continue
    last_hr = hr
    o = occ_at(t)
    row = []
    for n in names:
        a = r["accounts"].get(n)
        cell = f"{effective(a):3.0f}{'*'*min(o.get(n,0),3)}" if a else "  -"
        row.append(cell.rjust(12))
    print(f"   {hr}h " + "  ".join(row))
print("   (* = one live lane on that account at the time)")

# ---- 6. capacity-aware counterfactual (tier-normalized) --------------------
# The fleet is heterogeneous: rateLimitTier default_claude_max_20x vs _5x.
# Re-judge each double-book swap in ABSOLUTE per-lane headroom
# (pro-units = (100-eff)/100 * capacity_x, shared across lanes+1).
CAP = {"yaz-myjli-com-max": 20, "yaz-tefillinconnection-org-max": 20, "default": 20}
def _cap(n):
    return CAP.get(n, 5)

def _abs_per_lane(n, a, o):
    return (100 - effective(a)) / 100 * _cap(n) / (o.get(n, 0) + 1)

n_db = better_idle = 0
for r in recs:
    if r.get("action") != "swap" or "double-book" not in (r.get("reason") or ""):
        continue
    tgt = r.get("target"); acc = r.get("accounts") or {}
    if not tgt or tgt not in acc:
        continue
    n_db += 1
    o = occ_at(ts(r["when"]))
    frm = (r.get("where") or {}).get("from")
    tgt_rem = _abs_per_lane(tgt, acc[tgt], o)
    if any(_abs_per_lane(nm, a, o) > tgt_rem
           for nm, a in acc.items()
           if nm not in (tgt, frm, "default") and a.get("5h", 100) < 95
           and o.get(nm, 0) == 0):
        better_idle += 1
print(f"\n== capacity-aware counterfactual: {better_idle}/{n_db} double-book swaps "
      f"({100*better_idle/max(1,n_db):.0f}%) had an IDLE alternative with more "
      f"per-lane absolute headroom than the chosen stack target")
