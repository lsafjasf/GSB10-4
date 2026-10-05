"""bench_10m.py - 基准：1000 万条键、进程内存预算 64 MiB（仅标准库）。

默认模式（测量）：
  * 确定性数据流（种子固定）喂给算子，内存上限 = 预算 - 当前RSS - 安全余量；
  * 流式消费结果：校验“严格递增”（=> 无重复且有序）、计数、滚动摘要；
  * 报告耗时、溢写 run 数、溢写文件总大小、进程峰值内存(VmHWM)。
--check 模式（对拍）：
  * 先跑上面的测量阶段（内存数据干净）；
  * 再把同一数据流装入内存 set 构建参考（线束内存，不计入预算），
    对参考序列计算同样的滚动摘要，与算子输出的摘要逐位比对。
"""
import argparse
import hashlib
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spilldedup as sd


def key_stream(n, distinct, seed):
    import random
    rng = random.Random(seed)
    for _ in range(n):
        yield b"k%09d" % rng.randrange(distinct)


def read_hwm_bytes():
    try:
        with open("/proc/self/status", "rb") as fh:
            for line in fh:
                if line.startswith(b"VmHWM:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def fmt(nbytes):
    return "%.2f MiB" % (nbytes / (1 << 20))


class Digester:
    """对键序列计算滚动摘要（长度前缀防拼接歧义）。"""

    def __init__(self):
        self._h = hashlib.blake2b(digest_size=32)
        self.count = 0

    def update(self, key: bytes):
        self._h.update(struct.pack(">Q", len(key)))
        self._h.update(key)
        self.count += 1

    def hexdigest(self):
        return self._h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10_000_000)
    ap.add_argument("--distinct", type=int, default=3_000_000)
    ap.add_argument("--budget-mb", type=int, default=64)
    ap.add_argument("--seed", type=int, default=20261005)
    ap.add_argument("--check", action="store_true",
                    help="测量后与内存参考集合对拍（参考集合占额外内存）")
    args = ap.parse_args()

    budget = args.budget_mb << 20
    rss0 = sd.process_rss_bytes()
    limit = sd.memory_limit_for_budget(budget)
    print("配置: n=%d distinct=%d seed=%d" % (args.n, args.distinct, args.seed))
    print("内存预算=%s | 解释器基线RSS=%s | 算子缓冲上限=%s"
          % (fmt(budget), fmt(rss0), fmt(limit)))

    # ---- 测量阶段：算子在 64MiB 预算内运行，输出流式消费 ----
    op = sd.SpillDedup(memory_limit_bytes=limit)
    digest = Digester()
    t0 = time.perf_counter()
    op.add_many(key_stream(args.n, args.distinct, args.seed))
    t1 = time.perf_counter()
    prev = None
    ordered = True
    for key in op.results():
        if prev is not None and key <= prev:
            ordered = False
        prev = key
        digest.update(key)
    t2 = time.perf_counter()
    hwm = read_hwm_bytes()          # 在任何大内存参考结构之前读取

    print("输入键数        : %d" % args.n)
    print("输出唯一键数    : %d" % digest.count)
    print("输出严格递增    : %s" % ("是" if ordered else "否(错误!)"))
    print("输出摘要        : %s" % digest.hexdigest())
    print("溢写 run 数     : %d" % op.spill_count)
    print("溢写文件总大小  : %s (%d 字节)" % (fmt(op.spill_bytes_total),
                                               op.spill_bytes_total))
    print("灌入耗时        : %.2f s" % (t1 - t0))
    print("归并输出耗时    : %.2f s" % (t2 - t1))
    print("总耗时          : %.2f s" % (t2 - t0))
    print("进程峰值内存    : %s (预算 %s) -> %s"
          % (fmt(hwm), fmt(budget), "达标" if hwm <= budget else "超标!"))

    ok = ordered and hwm <= budget

    if args.check:
        # ---- 对拍阶段：参考集合是线束内存，不影响上面的测量 ----
        t3 = time.perf_counter()
        ref_digest = Digester()
        for key in sorted(set(key_stream(args.n, args.distinct, args.seed))):
            ref_digest.update(key)
        t4 = time.perf_counter()
        same = (ref_digest.hexdigest() == digest.hexdigest()
                and ref_digest.count == digest.count)
        print("对拍参考构建    : %.2f s（线束内存，不计入预算）" % (t4 - t3))
        print("参考摘要        : %s" % ref_digest.hexdigest())
        print("对拍结果        : %s"
              % ("完全一致 (%d 条)" % ref_digest.count if same else "不一致!"))
        ok = ok and same

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
