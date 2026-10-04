"""Scenario demo: prints the policy comparison, state before/after diff
and ownership trail. Also writes reports/demo_report.txt.

Run: python3 demo.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sticky import NoHealthyBackend, SessionRejected, StickyRouter

BACKENDS = ["b0", "b1", "b2", "b3"]
DEMO_SESSIONS = ["s-alpha", "s-bravo", "s-charlie", "s-delta", "s-echo"]


def build(policy):
    r = StickyRouter(BACKENDS, policy=policy)
    for sid in DEMO_SESSIONS:
        r.select(sid)
    for sid in DEMO_SESSIONS:
        r.put_state(sid, {"cart": [f"item-{sid[-1]}"], "hits": len(sid)})
    return r


def banner(out, text):
    out(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def main():
    lines = []
    out = lines.append

    banner(out, "SCENARIO 1 - all healthy: initial deterministic placement")
    r = build("migrate")
    out(f"{'session':12} {'owner':6} {'revision':>8} {'checksum(sha256)':>20}  data")
    before = {}
    for sid in DEMO_SESSIONS:
        snap = r.get_state(sid)
        before[sid] = (snap.revision, snap.data, snap.checksum, r.pins[sid])
        out(f"{sid:12} {r.pins[sid]:6} {snap.revision:8} {snap.checksum[:16]:>20}  {json.dumps(snap.data)}")

    victim = "b1"
    banner(out, f"SCENARIO 2 - backend {victim} removed: migrate policy vs reject policy")
    orphaned = sorted(sid for sid, (_, _, _, owner) in before.items() if owner == victim)
    out(f"pinned-to-{victim} sessions: {orphaned}")

    rm = build("migrate")
    rm.set_health(victim, False)
    out("\n[migrate policy]")
    for sid in orphaned:
        dst = rm.select(sid)
        rec = rm.history[-1]
        out(f"  {sid}: {victim} -> {dst}   checksum {rec.checksum_before[:12]} == {rec.checksum_after[:12]}")

    rr = build("reject")
    rr.set_health(victim, False)
    out("[reject policy]")
    for sid in orphaned:
        try:
            rr.select(sid)
        except SessionRejected as exc:
            out(f"  {sid}: REJECTED ({exc})")
    out(f"  pins after reject attempt: {{s: rr.pins[s] for s in orphaned}} = "
        f"{ {s: rr.pins[s] for s in orphaned} }")

    banner(out, "SCENARIO 3 - state before/after diff (migrate policy)")
    out(f"{'session':12} {'before owner':12} {'after owner':11} {'rev':>3} "
        f"{'checksum equal':>14}  data equal")
    all_ok = True
    for sid in DEMO_SESSIONS:
        rev, data, checksum, old_owner = before[sid]
        new_owner = rm.pins[sid]
        snap_after = rm.backends[new_owner].get(sid)
        same_sum = snap_after.checksum == checksum
        same_data = snap_after.data == data and snap_after.revision == rev
        all_ok &= same_sum and same_data
        out(f"{sid:12} {old_owner:12} {new_owner:11} {rev:3} {str(same_sum):>14}  {same_data}")
    out(f"\nRESULT: zero state loss across all {len(DEMO_SESSIONS)} sessions: {all_ok}")

    # One more write post-migration proves state keeps evolving on the new owner.
    sid = orphaned[0]
    rm.put_state(sid, {"cart": ["post-migration"], "hits": 99})
    snap = rm.get_state(sid)
    out(f"post-migration write on {sid}: owner={rm.pins[sid]} revision={snap.revision} "
        f"chained_from={snap.prev_checksum[:12]}")

    banner(out, "SCENARIO 4 - recovery sequence: ownership trail")
    rt = StickyRouter(BACKENDS, policy="migrate")
    sid = "s-trail"
    trail = [("initial", rt.select(sid))]
    rt.put_state(sid, {"phase": "initial"})
    first = rt.pins[sid]
    rt.set_health(first, False)
    trail.append((f"remove {first}", rt.select(sid)))
    rt.put_state(sid, {"phase": "after-first-drift"})
    second = rt.pins[sid]
    rt.set_health(first, True)
    trail.append((f"recover {first}", rt.select(sid)))
    rt.set_health(second, False)
    trail.append((f"remove {second}", rt.select(sid)))
    third = rt.pins[sid]
    rt.set_health(second, True)
    trail.append((f"recover {second}", rt.select(sid)))
    for event, owner in trail:
        out(f"  {event:16} -> owner {owner}")
    final = rt.get_state(sid)
    out(f"final state on {rt.pins[sid]}: {json.dumps(final.data)} revision={final.revision}")
    out("recovered backends never steal a migrated pin (recovery only flips health).")
    out("note: 'remove b2 -> b1' is a *migration* (owner died), not a steal-back;")
    out("      a recovered backend may host sessions only via new placement/migration.")

    banner(out, "SCENARIO 5 - all backends unavailable")
    rd = build("migrate")
    for b in BACKENDS:
        rd.set_health(b, False)
    try:
        rd.select("s-alpha")
    except NoHealthyBackend as exc:
        out(f"[migrate] {type(exc).__name__}: {exc}")
    for b in BACKENDS:
        rr.set_health(b, False)
    try:
        rr.select("s-alpha")
    except SessionRejected as exc:
        out(f"[reject]  {type(exc).__name__}: {exc}")
    rr.set_health("b0", True)
    out(f"after b0 recovery, unpinned new session -> {rr.select('s-brand-new')}")

    text = "\n".join(lines)
    print(text)
    os.makedirs("reports", exist_ok=True)
    with open("reports/demo_report.txt", "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(f"\n[written] reports/demo_report.txt")


if __name__ == "__main__":
    main()
