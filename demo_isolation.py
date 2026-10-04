"""Prints a real isolation run: mixed healthy/failing subscribers.

Run:  python3 demo_isolation.py
"""

from pubsub import EventBus

bus = EventBus()
received = []

bus.subscribe("news", lambda e: received.append(("alice", e)), name="alice")
bus.subscribe("news", lambda e: (_ for _ in ()).throw(ValueError("bad payload")),
              name="broken-bob")
bus.subscribe("news", lambda e: received.append(("carol", e)), name="carol")
bus.subscribe("news", lambda e: (_ for _ in ()).throw(KeyError("cfg")),
              name="broken-dave")
bus.subscribe("news", lambda e: received.append(("erin", e)), name="erin")

report = bus.publish("news", {"headline": "isolation works"})

print("== delivery result ==")
print("healthy subscribers received:", received)
print("delivered tokens            :", report.delivered)
print("failed count                :", report.failed_count)
print()
print("== aggregated errors returned to publisher ==")
print(report.describe_errors())
