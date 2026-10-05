# -*- coding: utf-8 -*-
"""性能基准:1 万状态、5 万转移,报告耗时与内存峰值。

机器构造:5000 状态随机基准机的两份同构副本(L/R),两副本同位置状态
互为等价 -> 强制产生 5000 个大小为 2 的等价类;并保证全部状态可达。
运行:python3 bench.py
"""

import random
import resource
import time
import tracemalloc

from fsm import FSM, analyze, minimize, partition_is_stable

N_BASE = 5_000
N_INPUTS = 5          # 每状态 5 条出边 -> 10_000 状态共 50_000 条转移
N_OUTPUTS = 3
SEED = 20261005


def build_machine():
    rng = random.Random(SEED)
    inputs = ["x%d" % i for i in range(N_INPUTS)]
    states = ["L%d" % i for i in range(N_BASE)] + \
             ["R%d" % i for i in range(N_BASE)]
    trans, out = {}, {}
    # 两副本共享同一套"目标选择":L_i 与 R_i 行为完全一致 -> 互为等价
    for i in range(N_BASE):
        for x in inputs:
            t = rng.randrange(N_BASE)
            copy = rng.choice("LR")
            tgt = "%s%d" % (copy, t)
            o = rng.randrange(N_OUTPUTS)
            trans[("L%d" % i, x)] = tgt
            trans[("R%d" % i, x)] = tgt
            out[("L%d" % i, x)] = o
            out[("R%d" % i, x)] = o
    # R0 也需可达:让 L0/R0 的首个输入都指向 R0(两副本仍行为一致)
    trans[("L0", inputs[0])] = "R0"
    trans[("R0", inputs[0])] = "R0"
    # 保证可达:为 i>=1 的两个副本各安排一条来自小编号状态的入边
    # (占用未使用的 (状态,输入) 槽位,避免互相覆盖)
    used = {(0, inputs[0])}
    for i in range(1, N_BASE):
        for copy in ("L", "R"):
            while True:
                j = rng.randrange(i)
                x = inputs[rng.randrange(N_INPUTS)]
                if (j, x) not in used:
                    used.add((j, x))
                    break
            tgt = "%s%d" % (copy, i)
            trans[("L%d" % j, x)] = tgt
            trans[("R%d" % j, x)] = tgt
    return FSM(states, inputs, "L0", trans, out)


def main():
    t0 = time.perf_counter()
    machine = build_machine()
    print("机器: %d 状态, %d 输入, %d 条转移, 输出字母表 %d (构建 %.3f s)"
          % (len(machine.states), N_INPUTS, len(machine.trans),
             N_OUTPUTS, time.perf_counter() - t0))

    reachable = machine.reachable_states()

    # 1) 干净计时(无内存探针)
    t0 = time.perf_counter()
    blocks, block_of = minimize(machine, reachable)
    stable = partition_is_stable(machine, blocks, block_of)
    t_min = time.perf_counter() - t0

    # 2) tracemalloc 测 Python 堆内存峰值
    tracemalloc.start()
    blocks2, block_of2 = minimize(machine, reachable)
    partition_is_stable(machine, blocks2, block_of2)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del blocks2, block_of2

    sizes = sorted((len(b) for b in blocks), reverse=True)
    nontrivial = sum(1 for s in sizes if s > 1)
    print("可达状态: %d / %d" % (len(reachable), len(machine.states)))
    print("等价类数: %d (非平凡类 %d 个, 最大类 %d 状态)"
          % (len(blocks), nontrivial, sizes[0]))
    print("划分稳定(等价论证成立): %s" % stable)
    print("核心判定(minimize+稳定性校验)耗时: %.3f s" % t_min)
    print("Python 堆内存峰值(tracemalloc): %.1f MiB" % (peak / 2**20))

    t0 = time.perf_counter()
    report = analyze(machine)
    t_all = time.perf_counter() - t0
    print("完整 analyze(含报告组装)耗时: %.3f s (等价类 %d, 不可达 %d, 缺失 %d)"
          % (t_all, report["class_count"], len(report["unreachable"]),
             len(report["missing_transitions"])))

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print("进程常驻内存峰值 ru_maxrss: %.1f MiB" % (rss_kb / 1024.0))


if __name__ == "__main__":
    main()
