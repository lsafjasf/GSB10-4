"""演示异常场景下的投递结果与异常汇总。运行：python3 demo.py"""

from pubsub import EventBus

bus = EventBus()
received = []

bus.subscribe("orders", lambda e: received.append(("alice", e)), name="alice")
bus.subscribe("orders", lambda e: (_ for _ in ()).throw(ValueError("db down")), name="bob")
bus.subscribe("orders", lambda e: received.append(("carol", e)), name="carol")
bus.subscribe("orders", lambda e: (_ for _ in ()).throw(RuntimeError("timeout")), name="dave")

result = bus.publish("orders", {"order_id": 42})

print("正常收到事件的订阅者:", received)
print("delivered ids        :", result.delivered)
print("errors (全部汇总)    :")
for err in result.errors:
    print(f"  sub#{err.subscription_id} {err.handler_name}: "
          f"{err.error_type}: {err.error}")
