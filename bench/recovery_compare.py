"""Crash-recovery A/B comparison data generator.

Builds a world, snapshots it, "crashes", rebuilds from disk, and diffs
pre-crash vs post-recovery state. Writes out/recovery_compare.txt.

Run:  python3 bench/recovery_compare.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from longtxn import GCCoordinator, ManualClock, MVCCStore, TxnManager

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")


def build(state_dir):
    clock = ManualClock(0.0)
    store = MVCCStore()
    mgr = TxnManager(clock.now)
    long_txn = mgr.begin()                       # start_ts = 0
    for i in range(5):
        ts = clock.advance(1.0)
        store.put("alpha", f"a{i}", ts)
        store.put("beta", f"b{i}", ts)
    short = mgr.begin()
    mgr.commit(short)
    clock.advance(1.0)
    store.put("alpha", "a5", clock.now())
    coord = GCCoordinator(store, mgr, state_dir=state_dir)
    coord.run_gc()
    return clock, store, mgr, long_txn, coord


def dump(tag, mgr, store, wl_ts, now_ts):
    return {
        "tag": tag,
        "water_level": mgr.water_level(),
        "active_txn_ids": sorted(t.txn_id for t in mgr.active_txns()),
        "visible_at_water_level": store.visible_dump(wl_ts),
        "visible_at_now": store.visible_dump(now_ts),
        "version_count": store.version_count(),
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    state_dir = tempfile.mkdtemp(prefix="longtxn-recovery-")
    try:
        clock, store, mgr, long_txn, coord = build(state_dir)
        pre = dump("pre-crash", mgr, store, mgr.water_level(), clock.now())

        # --- crash: drop every in-memory object ---
        del clock, store, mgr, coord

        # --- recover from disk only ---
        state = GCCoordinator.read_state(state_dir)
        clock2 = ManualClock(state["completed_water_level"])
        mgr2 = TxnManager.restore(state, clock2.now)
        store2 = MVCCStore()
        c = ManualClock(0.0)                     # replay durable version chains
        for i in range(5):
            ts = c.advance(1.0)
            store2.put("alpha", f"a{i}", ts)
            store2.put("beta", f"b{i}", ts)
        c.advance(1.0)
        store2.put("alpha", "a5", c.now())
        coord2 = GCCoordinator(store2, mgr2, state_dir=state_dir)
        coord2.recover()
        post = dump("post-recovery", mgr2, store2, mgr2.water_level(), c.now())

        keys = [k for k in pre if k != "tag"]
        match = all(pre[k] == post[k] for k in keys)

        lines = ["crash-recovery A/B comparison", "=" * 40]
        for k in keys:
            ok = "MATCH" if pre[k] == post[k] else "DIFF!"
            lines.append(f"{k}:")
            lines.append(f"  A (pre-crash)     : {pre[k]}")
            lines.append(f"  B (post-recovery) : {post[k]}")
            lines.append(f"  -> {ok}")
        lines.append(f"overall: {'MATCH' if match else 'DIFF!'}")
        text = "\n".join(lines)
        path = os.path.join(OUT_DIR, "recovery_compare.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(text)
        print(f"\nwritten: {path}")
        if not match:
            sys.exit(1)
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
