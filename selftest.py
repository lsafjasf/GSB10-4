"""自测 + 逐步骤双端对拍 + 异常检出演示。

运行：python3 selftest.py
输出：results/step_log.txt      每一步操作后编码端/解码端表内容对拍
      results/detection_log.txt 丢失/乱序/越界/溢出等检出记录
退出码非 0 表示有断言失败。
"""

import os
import sys
import unittest

from codec import (
    Encoder, Decoder,
    UpdateLost, UpdateReordered, MissingReference, UnsafeEviction,
)
from dynamic_table import (
    DynamicTable, entry_size,
    CapacityExceeded, EntryTooLarge, IndexOutOfBounds,
)

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
os.makedirs(RESULTS, exist_ok=True)


def fmt_table(tag, table):
    lines = [f"  [{tag}] capacity={table.capacity} size={table.size} "
             f"count={table.count} insert_count={table.insert_count} "
             f"dropped={table.dropped}"]
    if table.count == 0:
        lines.append("    (空表)")
    for rel, abs_i, name, value in table.snapshot():
        lines.append(f"    rel={rel:<2} abs={abs_i:<3} {name}: {value}")
    return lines


def run_scenario(log):
    """脚本化场景：容量设置、空表查询、持续插入触发 FIFO 驱逐、满表、
    头部块编解码，每一步之后对拍两端表内容。"""
    enc = Encoder(max_capacity=256)
    dec = Decoder(max_capacity=256)
    step = 0

    def checkpoint(title):
        nonlocal step
        step += 1
        log.append(f"步骤 {step:02d} | {title}")
        log.extend(fmt_table("编码端", enc.table))
        log.extend(fmt_table("解码端", dec.table))
        same = enc.table.snapshot() == dec.table.snapshot()
        log.append(f"  对拍结果: {'一致 OK' if same else '不一致 MISMATCH!'}")
        log.append("")
        assert same, f"步骤 {step} 两端表不一致"
        assert enc.table.size == dec.table.size
        assert enc.table.insert_count == dec.table.insert_count

    def do(ins):
        dec.apply(ins)

    def roundtrip(title, headers):
        block = enc.encode(headers)
        log.append(f"[{title}] 头部块前缀: RIC={block[0]} base={block[1]} fields={block[2]}")
        decoded, krc = dec.decode(block)
        enc.on_ack(krc)
        log.append(f"[{title}] 解码结果: {decoded}")
        log.append(f"[{title}] 解码端回执 Known Received Count = {krc}")
        assert decoded == headers
        log.append("")

    # 1. 空表状态下查询 -> 越界
    try:
        dec.table.get_relative(0)
        raise AssertionError("空表查询应抛 IndexOutOfBounds")
    except IndexOutOfBounds as e:
        log.append(f"空表查询检出: IndexOutOfBounds: {e}")
        log.append("")
    checkpoint("初始空表（容量 0）")

    # 2. 设置容量 220
    do(enc.set_capacity(220))
    checkpoint("Set Dynamic Table Capacity = 220")

    # 3-6. 连续插入四条（48+54+57+55=214 <= 220，无驱逐）
    do(enc.insert(":path", "/index.html"))
    checkpoint("插入 :path: /index.html (48B)")
    do(enc.insert("custom-key", "custom-value"))
    checkpoint("插入 custom-key: custom-value (54B)")
    do(enc.insert("user-agent", "gsb-client/1.0"))
    checkpoint("插入 user-agent: gsb-client/1.0 (57B)")
    do(enc.insert("accept-encoding", "gzip, br"))
    checkpoint("插入 accept-encoding: gzip, br (55B)")

    # 7. 头部块往返 #1，解码端确认收到前 4 条（KRC=4），后续才能驱逐
    roundtrip("往返#1", [("custom-key", "custom-value"),
                        ("user-agent", "gsb-client/1.0"),
                        ("cache-control", "no-cache")])
    checkpoint("头部块往返 #1（编码端记录 KRC=4）")

    # 8. 再插入一条 45B：214+45>220，驱逐最旧的 abs=0（已确认，安全）
    do(enc.insert("x-token", "abcdef"))
    checkpoint("插入 x-token: abcdef (45B)，FIFO 驱逐 abs=0")

    # 9. 头部块往返 #2，确认收到 abs=4（KRC=5）
    roundtrip("往返#2", [("x-token", "abcdef")])
    checkpoint("头部块往返 #2（编码端记录 KRC=5）")

    # 10. 缩小容量到 60：立即驱逐 abs=1,2,3（均已确认），保留 x-token 45B
    do(enc.set_capacity(60))
    checkpoint("Set Dynamic Table Capacity = 60（强制驱逐已确认旧项）")

    # 11. 插入一条恰好 60B 的表项：驱逐 abs=4（已确认），表恰好占满
    do(enc.insert("k" * 14, "v" * 14))
    checkpoint("插入 60B 表项，FIFO 驱逐 abs=4，容量恰好占满")

    # 12. 表项大于容量 -> 指令无法执行，表状态不变
    try:
        enc.insert("big", "x" * 100)
        raise AssertionError("应抛 EntryTooLarge")
    except EntryTooLarge as e:
        log.append(f"溢出检出: EntryTooLarge: {e}")
        log.append("")
    checkpoint("尝试插入 132B 表项（被拒，表保持不变）")

    # 13. 容量超协商上限 -> 指令无法执行
    try:
        enc.set_capacity(999)
        raise AssertionError("应抛 CapacityExceeded")
    except CapacityExceeded as e:
        log.append(f"容量上限检出: CapacityExceeded: {e}")
        log.append("")
    checkpoint("尝试 Set Capacity = 999 > max 256（被拒）")

    # 14. 绝对索引越界（引用已被驱逐的表项）
    try:
        dec.table.get_absolute(0)
        raise AssertionError("应抛 IndexOutOfBounds")
    except IndexOutOfBounds as e:
        log.append(f"索引越界检出: IndexOutOfBounds: {e}")
        log.append("")
    checkpoint("查询已被驱逐的绝对索引 0（被拒）")

    # 15. 未确认表项不可驱逐：新 60B 表项 (abs=5) 尚未被引用确认
    try:
        enc.insert("z" * 14, "w" * 14)
        raise AssertionError("应抛 UnsafeEviction")
    except UnsafeEviction as e:
        log.append(f"不可驱逐检出: UnsafeEviction: {e}")
        log.append("")
    checkpoint("尝试驱逐未确认表项 abs=5（被拒，表保持不变）")

    return step


def run_detection(log):
    """丢失 / 乱序 / 引用缺失 的解码层检出。"""
    # A. 指令丢失：丢掉 seq=1
    enc = Encoder(max_capacity=256)
    dec = Decoder(max_capacity=256)
    i0 = enc.set_capacity(220)
    i1 = enc.insert("a", "1")
    i2 = enc.insert("b", "2")
    dec.apply(i0)
    log.append("[丢失] 丢弃指令 seq=1 (insert a:1)，直接投递 seq=2")
    try:
        dec.apply(i2)
        raise AssertionError("应抛 UpdateLost")
    except UpdateLost as e:
        log.append(f"  检出: UpdateLost: {e}")
    log.append("")

    # B. 指令乱序：seq=2 先于 seq=1 到达
    enc = Encoder(max_capacity=256)
    dec = Decoder(max_capacity=256)
    i0 = enc.set_capacity(220)
    i1 = enc.insert("a", "1")
    i2 = enc.insert("b", "2")
    dec.apply(i0)
    dec.apply(i1)
    log.append("[乱序] 先投递 seq=2 再投递 seq=1（重复旧序号）")
    dec.apply(i2)
    try:
        dec.apply(i1)
        raise AssertionError("应抛 UpdateReordered")
    except UpdateReordered as e:
        log.append(f"  检出: UpdateReordered: {e}")
    log.append("")

    # C. 引用缺失：头部块在插入指令之前到达（RIC 检查拦截）
    enc = Encoder(max_capacity=256)
    dec = Decoder(max_capacity=256)
    i0 = enc.set_capacity(220)
    i1 = enc.insert("token", "xyz")
    block = enc.encode([("token", "xyz")])  # 引用绝对索引 0，RIC=1
    dec.apply(i0)
    log.append("[引用缺失] 头部块(RIC=1)先于 insert 指令到达，解码端 insert_count=0")
    try:
        dec.decode(block)
        raise AssertionError("应抛 MissingReference")
    except MissingReference as e:
        log.append(f"  检出: MissingReference: {e}")
    log.append("  补偿：补投 insert 指令后重试解码")
    dec.apply(i1)
    headers, krc = dec.decode(block)
    log.append(f"  重试成功: {headers}，Known Received Count={krc}")
    log.append("")

    # D. 头部块内索引越界（伪造块引用不存在的表项）
    log.append("[索引越界] 伪造头部块引用绝对索引 99")
    try:
        dec.decode((100, 100, [("indexed", 0)]))
        raise AssertionError("应抛 MissingReference 或 IndexOutOfBounds")
    except (MissingReference, IndexOutOfBounds) as e:
        log.append(f"  检出: {type(e).__name__}: {e}")
    log.append("")


class TestDynamicTable(unittest.TestCase):
    def test_empty_table(self):
        t = DynamicTable(128)
        self.assertEqual(t.count, 0)
        with self.assertRaises(IndexOutOfBounds):
            t.get_relative(0)
        with self.assertRaises(IndexOutOfBounds):
            t.get_absolute(0)

    def test_entry_size_and_capacity(self):
        t = DynamicTable(64)
        t.set_capacity(64)
        t.insert("ab", "cd")  # 2+2+32=36
        self.assertEqual(t.size, 36)
        with self.assertRaises(EntryTooLarge):
            t.insert("x" * 40, "y")  # 73 > 64
        with self.assertRaises(CapacityExceeded):
            t.set_capacity(65)

    def test_fifo_eviction(self):
        t = DynamicTable(100)
        t.set_capacity(100)
        t.insert("a", "1")  # 34
        t.insert("b", "2")  # 34
        t.insert("c", "3")  # 34 -> 102>100, 驱逐 a
        self.assertEqual(t.dropped, 1)
        self.assertEqual(t.count, 2)
        self.assertEqual(t.get_relative(0), ("c", "3"))
        self.assertEqual(t.get_relative(1), ("b", "2"))
        with self.assertRaises(IndexOutOfBounds):
            t.get_absolute(0)  # a 已驱逐

    def test_continuous_insert(self):
        t = DynamicTable(128)
        t.set_capacity(128)
        for i in range(1000):
            t.insert(f"k{i}", "v")
        self.assertLessEqual(t.size, 128)
        self.assertEqual(t.insert_count, 1000)
        self.assertEqual(t.dropped + t.count, 1000)

    def test_shrink_capacity_evicts(self):
        t = DynamicTable(256)
        t.set_capacity(200)
        t.insert("a", "1")
        t.insert("b", "2")
        t.set_capacity(40)
        self.assertEqual(t.count, 1)
        self.assertEqual(t.dropped, 1)

    def test_index_bounds(self):
        t = DynamicTable(64)
        t.set_capacity(64)
        t.insert("a", "1")
        with self.assertRaises(IndexOutOfBounds):
            t.get_relative(1)
        with self.assertRaises(IndexOutOfBounds):
            t.get_relative(-1)
        with self.assertRaises(IndexOutOfBounds):
            t.get_absolute(1)

    def test_end_to_end_roundtrip(self):
        enc = Encoder(max_capacity=256)
        dec = Decoder(max_capacity=256)
        dec.apply(enc.set_capacity(220))
        for name, value in [("a", "1"), ("b", "2"), ("c", "3")]:
            dec.apply(enc.insert(name, value))
        block = enc.encode([("a", "1"), ("c", "3"), ("d", "4")])
        headers, krc = dec.decode(block)
        self.assertEqual(headers, [("a", "1"), ("c", "3"), ("d", "4")])
        enc.on_ack(krc)
        self.assertEqual(enc.table.snapshot(), dec.table.snapshot())

    def test_loss_and_reorder_detected(self):
        enc = Encoder(max_capacity=64)
        dec = Decoder(max_capacity=64)
        i0 = enc.set_capacity(64)
        i1 = enc.insert("a", "1")
        dec.apply(i0)
        with self.assertRaises(UpdateLost):
            dec.apply(("insert", 5, "x", "y"))
        dec.apply(i1)
        with self.assertRaises(UpdateReordered):
            dec.apply(i1)

    def test_missing_reference_detected(self):
        enc = Encoder(max_capacity=64)
        dec = Decoder(max_capacity=64)
        dec.apply(enc.set_capacity(64))
        ins = enc.insert("t", "v")
        block = enc.encode([("t", "v")])
        with self.assertRaises(MissingReference):
            dec.decode(block)  # insert 还没到
        dec.apply(ins)
        headers, _ = dec.decode(block)
        self.assertEqual(headers, [("t", "v")])


def main():
    step_log, det_log = [], []
    steps = run_scenario(step_log)
    run_detection(det_log)

    with open(os.path.join(RESULTS, "step_log.txt"), "w", encoding="utf-8") as f:
        f.write("动态表逐步骤双端对拍（编码端 vs 解码端）\n")
        f.write("=" * 60 + "\n")
        f.write("\n".join(step_log) + "\n")
    with open(os.path.join(RESULTS, "detection_log.txt"), "w", encoding="utf-8") as f:
        f.write("异常检出记录（丢失 / 乱序 / 引用缺失 / 越界）\n")
        f.write("=" * 60 + "\n")
        f.write("\n".join(det_log) + "\n")

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestDynamicTable)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"\n对拍场景共 {steps} 步，全部一致；日志见 results/step_log.txt")
    print(f"检出记录见 results/detection_log.txt")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
