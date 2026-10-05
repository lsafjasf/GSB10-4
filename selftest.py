# -*- coding: utf-8 -*-
"""健康状态传播库自测。运行: python3 selftest.py  (数据写入 results/)"""

import hashlib
import os
import random
import resource
import sys
import time
import tracemalloc

from health_propagation import (ClusterHealth, Status, Update, Verdict)

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
os.makedirs(RESULTS, exist_ok=True)

# 统一测试配置：窗口 8，连续 2 次不健康摘除，连续 3 次健康恢复，
# 窗口内翻转 >= 4 次判定抖动。恢复(3)比摘除(2)更保守。
CFG = dict(window=8, remove_threshold=2, recover_threshold=3, flap_threshold=4)


def flap_signal():
    """4 次健康 -> 24 次来回抖动 -> 12 次稳定健康。"""
    return [True] * 4 + [i % 2 == 0 for i in range(24)] + [True] * 12


def count_disruptions(statuses):
    """UP -> 非UP 的次数 = 上游连接被打散的次数。"""
    n = 0
    for a, b in zip(statuses, statuses[1:]):
        if a is Status.UP and b is not Status.UP:
            n += 1
    return n


def test_single_node_flap():
    print("== 1. 单节点抖动：抑制前后状态序列对比 ==")
    raw = flap_signal()

    # 抑制前：同样的摘除/恢复阈值，但关闭抖动抑制
    no_sup = ClusterHealth(window=CFG["window"], remove_threshold=2,
                           recover_threshold=2, flap_threshold=None)
    # 抑制后：摘除/恢复阈值完全相同，仅开启抖动抑制
    sup = ClusterHealth(window=CFG["window"], remove_threshold=2,
                        recover_threshold=2, flap_threshold=4)

    seq_no, seq_sup = [], []
    for i, h in enumerate(raw):
        _, s1 = no_sup.apply(Update(0, i, h))
        _, s2 = sup.apply(Update(0, i, h))
        seq_no.append(s1)
        seq_sup.append(s2)

    path = os.path.join(RESULTS, "single_node_flap.csv")
    with open(path, "w") as f:
        f.write("seq,raw_healthy,status_without_suppression,status_with_suppression\n")
        for i, h in enumerate(raw):
            f.write("%d,%s,%s,%s\n" % (i, int(h), seq_no[i].value, seq_sup[i].value))

    d_no, d_sup = count_disruptions(seq_no), count_disruptions(seq_sup)
    print("  抑制前状态序列: %s" % " ".join(s.value[0] for s in seq_no))
    print("  抑制后状态序列: %s" % " ".join(s.value[0] for s in seq_sup))
    print("  (U=UP D=DOWN S=SUPPRESSED)")
    print("  上游连接被打散次数: 抑制前 %d -> 抑制后 %d" % (d_no, d_sup))
    print("  数据已写入 %s" % path)

    assert Status.SUPPRESSED in seq_sup, "抖动期应进入 SUPPRESSED"
    assert Status.SUPPRESSED not in seq_no
    assert seq_sup[-1] is Status.UP, "稳定后应恢复 UP"
    assert d_sup < d_no, "抑制后打散次数必须显著下降"
    print("  PASS\n")


def test_all_nodes_flap():
    print("== 2. 全员抖动：1000 节点同时抖动后同时恢复 ==")
    n_nodes = 1000
    raw = flap_signal()
    c = ClusterHealth(**CFG)
    mid = len(raw) - 12  # 抖动段结束、稳定段开始
    for i in range(len(raw)):
        for nid in range(n_nodes):
            c.apply(Update(nid, i, raw[i]))
        if i == mid - 1:
            snap = c.snapshot()
            assert all(s is Status.SUPPRESSED for s in snap.values()), \
                "抖动段末尾所有节点应处于 SUPPRESSED"
            print("  抖动段末尾: 1000/1000 节点 SUPPRESSED")
    snap = c.snapshot()
    assert all(s is Status.UP for s in snap.values()), "稳定健康后所有节点应恢复 UP"
    print("  稳定段末尾: 1000/1000 节点恢复 UP")
    print("  PASS\n")


def test_convergence():
    print("== 3. 收敛对拍：同一批更新 6 种顺序，最终状态完全一致 ==")
    rng = random.Random(7)
    n_nodes, per = 100, 50
    updates = [Update(n, s, rng.random() < 0.7)
               for n in range(n_nodes) for s in range(per)]
    # 注入 10% 重复更新，重复不得改变结果
    updates += [updates[rng.randrange(len(updates))]
                for _ in range(len(updates) // 10)]

    def run(order):
        c = ClusterHealth(**CFG)
        for u in order:
            c.apply(u)
        return c.snapshot()

    base = run(sorted(updates, key=lambda u: (u.node_id, u.seq)))
    lines = ["order,sha1_of_snapshot,identical_to_inorder"]
    base_h = hashlib.sha1(repr(sorted(base.items())).encode()).hexdigest()
    lines.append("in-order,%s,True" % base_h)
    print("  顺序到达   sha1=%s" % base_h[:12])

    for trial in range(5):
        shuffled = updates[:]
        random.Random(100 + trial).shuffle(shuffled)
        snap = run(shuffled)
        h = hashlib.sha1(repr(sorted(snap.items())).encode()).hexdigest()
        ok = snap == base
        lines.append("shuffle-%d,%s,%s" % (trial, h, ok))
        print("  随机顺序 %d sha1=%s 一致=%s" % (trial, h[:12], ok))
        assert ok, "不同到达顺序必须收敛到同一状态"

    path = os.path.join(RESULTS, "convergence.txt")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("  对拍数据已写入 %s" % path)
    print("  PASS\n")


def test_stale_and_duplicate():
    print("== 4. 过期 / 重复 / 迟到乱序识别 ==")
    c = ClusterHealth(**CFG)
    for s in range(20):
        c.apply(Update(0, s, True))

    v, _ = c.apply(Update(0, 17, False))   # 窗口内已有序号
    assert v is Verdict.DUPLICATE, v
    print("  重复序号 17        -> DUPLICATE（健康位不同也忽略）")

    v, _ = c.apply(Update(0, 3, False))    # 已滑出窗口(窗口8, 覆盖12..19)
    assert v is Verdict.EXPIRED, v
    print("  过期序号 3         -> EXPIRED（旧状态不得当新状态用）")

    # 迟到乱序：先跳过 seq=5，之后补发
    c2 = ClusterHealth(**CFG)
    for s in list(range(0, 5)) + list(range(6, 12)):
        c2.apply(Update(1, s, s % 3 != 0))
    v, _ = c2.apply(Update(1, 5, False))
    assert v is Verdict.STALE_MERGED, v
    # 与按顺序到达的结果必须一致
    c3 = ClusterHealth(**CFG)
    for s in range(12):
        c3.apply(Update(1, s, s % 3 != 0))
    assert c2.status(1) == c3.status(1)
    print("  迟到序号 5         -> STALE_MERGED，并入后与顺序到达状态一致(%s)"
          % c2.status(1).value)

    # 旧状态不得改变当前状态
    before = c.status(0)
    c.apply(Update(0, 3, False))
    assert c.status(0) is before
    print("  过期更新后状态保持不变: %s" % before.value)
    print("  PASS\n")


def test_symmetry():
    print("== 5. 摘除/恢复对称性：恢复不得比摘除更激进 ==")
    c = ClusterHealth(**CFG)
    for s in range(5):
        c.apply(Update(0, s, True))
    assert c.status(0) is Status.UP

    c.apply(Update(0, 5, False))
    assert c.status(0) is Status.UP, "1 次不健康不得摘除"
    c.apply(Update(0, 6, False))
    assert c.status(0) is Status.DOWN, "连续 2 次不健康 = remove_threshold，摘除"
    print("  摘除: 连续不健康 1 次->UP, 2 次(=remove_threshold)->DOWN")

    c.apply(Update(0, 7, True))
    assert c.status(0) is Status.DOWN
    c.apply(Update(0, 8, True))
    assert c.status(0) is Status.DOWN, "恢复阈值更保守: 2 次健康仍 DOWN"
    c.apply(Update(0, 9, True))
    assert c.status(0) is Status.UP, "连续 3 次健康 = recover_threshold，恢复"
    print("  恢复: 连续健康 1,2 次->仍DOWN, 3 次(=recover_threshold)->UP")

    try:
        ClusterHealth(window=8, remove_threshold=3, recover_threshold=2)
        raise AssertionError("recover < remove 必须被拒绝")
    except ValueError as e:
        print("  构造校验: recover<remove 被拒绝 (%s)" % e)
    print("  PASS\n")


def test_edge_cases():
    print("== 6. 边界用例 ==")
    # 未知节点默认健康
    c = ClusterHealth(**CFG)
    assert c.status(999) is Status.UP and c.healthy(999)
    print("  未知节点默认 UP")

    # 参数校验
    for bad in (dict(window=1),
                dict(window=8, remove_threshold=0),
                dict(window=8, remove_threshold=9),
                dict(window=8, flap_threshold=8),
                dict(window=8, flap_threshold=0)):
        try:
            ClusterHealth(**bad)
            raise AssertionError("非法参数应被拒绝: %s" % bad)
        except ValueError:
            pass
    print("  非法参数(window=1, 阈值越界等)均被 ValueError 拒绝")

    # 抖动阈值边界：窗口内翻转恰好 == flap_threshold 才抑制
    c = ClusterHealth(window=8, remove_threshold=2, recover_threshold=2,
                      flap_threshold=4)
    sig = [True, False, True, False, True, True, True, True]  # 4 次翻转
    st = None
    for i, h in enumerate(sig):
        _, st = c.apply(Update(0, i, h))
    assert st is Status.SUPPRESSED, "翻转 4 次(==阈值)应抑制"
    print("  翻转次数 == flap_threshold 时恰好触发 SUPPRESSED")

    # 抑制恢复：抖动退出窗口 + 连续健康 >= recover_threshold 才恢复
    c = ClusterHealth(**CFG)
    sig = [True, False] * 6 + [True] * 12
    st = None
    for i, h in enumerate(sig):
        _, st = c.apply(Update(0, i, h))
    assert st is Status.UP
    print("  抖动退出窗口且连续健康达标后恢复 UP")

    # 重复更新带相反健康位也不得生效
    c = ClusterHealth(**CFG)
    c.apply(Update(0, 0, True))
    v, s = c.apply(Update(0, 0, False))
    assert v is Verdict.DUPLICATE and s is Status.UP
    print("  同序号相反健康位 -> DUPLICATE，状态不被篡改")
    print("  PASS\n")


def benchmark():
    print("== 7. 基准: 1000 节点, 1,000,000 次更新 ==")
    n_nodes, total = 1000, 1_000_000
    rng = random.Random(2026)
    c = ClusterHealth(window=16, remove_threshold=3,
                      recover_threshold=3, flap_threshold=6)
    counters = [0] * n_nodes
    verdicts = {v: 0 for v in Verdict}

    tracemalloc.start()
    t0 = time.perf_counter()
    for _ in range(total):
        nid = rng.randrange(n_nodes)
        if counters[nid] >= 4 and rng.random() < 0.05:
            # 旧序号重放（窗口=16，在最近 20 个序号里取）：
            # 已见->DUPLICATE，跳过的洞->STALE_MERGED，滑出窗口->EXPIRED
            seq = rng.randrange(max(0, counters[nid] - 20), counters[nid])
        else:
            seq = counters[nid]
            counters[nid] += 1
            if rng.random() < 0.05:
                counters[nid] += 1  # 跳号，留下稍后可能被补发的洞
        v, _ = c.apply(Update(nid, seq, rng.random() < 0.75))
        verdicts[v] += 1
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    lines = [
        "python=%s" % sys.version.split()[0],
        "nodes=%d updates=%d" % (n_nodes, total),
        "elapsed_sec=%.3f" % elapsed,
        "updates_per_sec=%.0f" % (total / elapsed),
        "tracemalloc_peak_mib=%.2f" % (peak / 1048576),
        "process_maxrss_mib=%.2f" % (rss_kb / 1024),
        "verdicts=%s" % {v.value: n for v, n in verdicts.items()},
    ]
    for ln in lines:
        print("  " + ln)
    path = os.path.join(RESULTS, "benchmark.txt")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("  数据已写入 %s" % path)
    print("  PASS\n")


def main():
    test_single_node_flap()
    test_all_nodes_flap()
    test_convergence()
    test_stale_and_duplicate()
    test_symmetry()
    test_edge_cases()
    benchmark()
    print("全部自测通过。")


if __name__ == "__main__":
    main()
