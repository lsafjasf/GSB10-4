"""迁移演示：4 -> 8 分区翻倍，迁移中持续写入，输出顺序对拍数据。

运行： python3 migration_demo.py
"""

from keyrouter import (
    MergingConsumer,
    PartitionedLog,
    Producer,
    Router,
)

OLD_N, NEW_N = 4, 8
KEYS = ["key-%02d" % i for i in range(12)]


def main():
    old_log, new_log = PartitionedLog(OLD_N), PartitionedLog(NEW_N)
    old_r, new_r = Router(OLD_N), Router(NEW_N)
    producer = Producer(old_r, new_r, old_log, new_log)
    consumer = MergingConsumer()

    # 阶段 1：迁移前（旧布局 4 分区）
    for i in range(120):
        producer.send(KEYS[i % len(KEYS)], payload="pre-%d" % i)
    consumer.poll(old_log)

    # 阶段 2：双写过渡，迁移中继续写入，交错读取制造乱序到达
    producer.mode = Producer.DUAL
    for i in range(240):
        producer.send(KEYS[i % len(KEYS)], payload="mid-%d" % i)
        if i % 5 == 0:
            consumer.poll(old_log, new_log)
    consumer.poll(old_log, new_log)
    assert consumer.consumed_all(old_log), "栅栏点：旧日志必须消费完才能切换"

    # 阶段 3：切换到新布局（8 分区）
    producer.mode = Producer.NEW
    for i in range(120):
        producer.send(KEYS[i % len(KEYS)], payload="post-%d" % i)
    consumer.poll(new_log)
    assert consumer.consumed_all(new_log)

    print(f"迁移: {OLD_N} -> {NEW_N} 分区；"
          f"写入 120(前) + 240(双写中) + 120(后) = 480 条")
    print()
    print("键 -> 分区映射变化（迁移前 -> 迁移后）：")
    for key in KEYS:
        print(f"  {key}: 分区 {old_r.partition_for(key)} -> "
              f"{new_r.partition_for(key)}")
    print()
    print("顺序对拍（每键交付序号必须严格等于 0..n-1，负载逐条一致）：")
    print(f"  {'键':<8} {'条数':>4} {'首序号':>6} {'末序号':>6} "
          f"{'序号连续':>6} {'负载一致':>6}")
    all_ok = True
    for key, count, first, last, ok in consumer.order_report():
        payload_ok = consumer.delivered[key] == producer.sent_payloads(key)
        all_ok &= ok and payload_ok
        print(f"  {key:<8} {count:>4} {first:>6} {last:>6} "
              f"{'PASS' if ok else 'FAIL':>6} "
              f"{'PASS' if payload_ok else 'FAIL':>6}")
    total = sum(len(v) for v in consumer.delivered.values())
    print()
    print(f"交付总数 {total}（双写 240 条经过去重，无重复交付）")
    print("总体结果:", "PASS —— 迁移全程无同键乱序" if all_ok else "FAIL")


if __name__ == "__main__":
    main()
