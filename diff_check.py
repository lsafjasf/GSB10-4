# -*- coding: utf-8 -*-
"""
diff_check.py —— 用 hashagg 复现 data/expected.tsv（对拍）

以极小预算（64KB，强制多级溢出）跑 data/rows.tsv，
结果必须与朴素实现生成的 data/expected.tsv 逐行一致。
"""
import sys

from hashagg import SpillAggregator

OPS = [("count", None), ("sum", 1), ("min", 2), ("max", 2)]


def load_rows(path="data/rows.tsv"):
    with open(path) as f:
        for line in f:
            k, a, b = line.split("\t")
            yield (int(k), int(a), int(b))


def load_expected(path="data/expected.tsv"):
    out = {}
    with open(path) as f:
        for line in f:
            k, c, s, mi, ma = line.split("\t")
            out[int(k)] = (int(c), int(s), int(mi), int(ma))
    return out


def main():
    expected = load_expected()
    with SpillAggregator(OPS, budget=64 * 1024) as agg:
        got = agg.collect(load_rows())
        stats = agg.stats
    if got != expected:
        diff = [k for k in expected if got.get(k) != expected[k]][:5]
        print("FAIL: 结果不一致, 例: %s" % diff)
        sys.exit(1)
    print("OK: %d 组与 data/expected.tsv 完全一致 "
          "(预算 64KB, 溢出事件=%d, 最大深度=%d, 叶子分区=%d)"
          % (len(got), len(stats["spills"]), stats["max_depth"],
             stats["leaf_partitions"]))


if __name__ == "__main__":
    main()
