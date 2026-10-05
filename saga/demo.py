"""Prints per-failure-point rollback results and idempotency evidence.

Run:  python3 demo.py
"""

import json
from collections import Counter

from saga import Executor, Stage, Store


def show(title, result, store):
    print(f"=== {title} ===")
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    print("external snapshot:", store.visible_snapshot(None))
    print()


# -- failure at stage 0 -----------------------------------------------------
store, ex = Store(), None
ex = Executor(store)
r = ex.run(
    [Stage("reserve", lambda c: (_ for _ in ()).throw(RuntimeError("no quota"))),
     Stage("charge", lambda c: c.write("charge", 100)),
     Stage("ship", lambda c: c.write("ship", "done"))],
    task_id="demo-fail-first",
)
show("failure at stage 0 (nothing committed, nothing to roll back)", r, store)

# -- failure at stage 2 (middle) ---------------------------------------------
store, ex = Store(), Executor(store)
store.baseline_write("stock", 10)
comp = Counter()
r = ex.run(
    [Stage("reserve", lambda c: c.write("stock", 9), lambda c: comp.__setitem__("reserve", comp["reserve"] + 1)),
     Stage("charge", lambda c: c.write("charge", 100), lambda c: comp.__setitem__("charge", comp["charge"] + 1)),
     Stage("ship", lambda c: (_ for _ in ()).throw(RuntimeError("courier down")))],
    task_id="demo-fail-mid",
)
show("failure at stage 2 (prefix compensated in reverse order)", r, store)
print("compensate hook invocations:", dict(comp))
print()

# -- idempotent repeated rollback ---------------------------------------------
for i in range(2):
    again = ex.rollback("demo-fail-mid", [
        Stage("reserve", lambda c: None, lambda c: comp.__setitem__("reserve", comp["reserve"] + 1)),
        Stage("charge", lambda c: None, lambda c: comp.__setitem__("charge", comp["charge"] + 1)),
        Stage("ship", lambda c: None),
    ])
    print(f"repeat rollback #{i + 1}:", [o.state for o in again],
          "| hook invocations still:", dict(comp),
          "| snapshot:", store.visible_snapshot(None))
print()

# -- failure during rollback, then retry ---------------------------------------
store, ex = Store(), Executor(store)
flaky = {"down": True}
calls = Counter()

def comp_reserve(ctx):
    calls["reserve"] += 1
    if flaky["down"]:
        raise RuntimeError("inventory service unreachable")

r = ex.run(
    [Stage("reserve", lambda c: c.write("stock", 9), comp_reserve),
     Stage("charge", lambda c: c.write("charge", 100), lambda c: calls.__setitem__("charge", calls["charge"] + 1)),
     Stage("ship", lambda c: (_ for _ in ()).throw(RuntimeError("courier down")))],
    task_id="demo-flaky",
)
show("compensation of stage 0 fails -> ROLLBACK_FAILED (rollback stops)", r, store)

flaky["down"] = False
retry = ex.rollback("demo-flaky", [
    Stage("reserve", lambda c: None, comp_reserve),
    Stage("charge", lambda c: None, lambda c: calls.__setitem__("charge", calls["charge"] + 1)),
    Stage("ship", lambda c: None),
])
print("retry rollback:", [(o.index, o.state, o.keys_restored) for o in retry])
print("hook invocations:", dict(calls), "| snapshot:", store.visible_snapshot(None))
