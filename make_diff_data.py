# -*- coding: utf-8 -*-
"""
make_diff_data.py —— 生成固定对拍数据集

data/rows.tsv     : 输入行（key<TAB>a<TAB>b），含单组、均匀组、倾斜组混合
data/expected.tsv : 朴素 dict 聚合的预期结果（key<TAB>count<TAB>sum<TAB>min<TAB>max）
任何对 hashagg 的实现改动，都必须复现这份结果。
"""
import os
import random

OUT_DIR = "data"
N = 100_000


def gen_rows():
    rng = random.Random(20261005)
    rows = []
    # 1) 单分组：1 万个固定 key=42
    for _ in range(10_000):
        rows.append((42, rng.randint(-999, 999), rng.randint(-999, 999)))
    # 2) 均匀多组：3 万个 key 均匀分布在 0..4999
    for _ in range(60_000):
        rows.append((rng.randrange(5000),
                     rng.randint(-10 ** 6, 10 ** 6),
                     rng.randint(-10 ** 6, 10 ** 6)))
    # 3) 极端倾斜：3 万行里 70% 是 key=1，其余指数衰减
    for _ in range(30_000):
        if rng.random() < 0.7:
            k = 1
        else:
            k = 5000 + int(rng.expovariate(1 / 200.0)) % 3000
        rows.append((k, rng.randint(-10 ** 6, 10 ** 6),
                     rng.randint(-10 ** 6, 10 ** 6)))
    rng.shuffle(rows)
    return rows


def naive(rows):
    out = {}
    for k, a, b in rows:
        c, s, mi, ma = out.get(k, (0, 0, None, None))
        out[k] = (c + 1, s + a,
                  b if mi is None else min(mi, b),
                  b if ma is None else max(ma, b))
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = gen_rows()
    with open(os.path.join(OUT_DIR, "rows.tsv"), "w") as f:
        for r in rows:
            f.write("%d\t%d\t%d\n" % r)
    exp = naive(rows)
    with open(os.path.join(OUT_DIR, "expected.tsv"), "w") as f:
        for k in sorted(exp):
            c, s, mi, ma = exp[k]
            f.write("%d\t%d\t%d\t%d\t%d\n" % (k, c, s, mi, ma))
    checksum = sum((c + s + (mi or 0) + (ma or 0)) for c, s, mi, ma in exp.values())
    with open(os.path.join(OUT_DIR, "CHECKSUM"), "w") as f:
        f.write("rows=%d\ngroups=%d\nchecksum=%d\n" % (len(rows), len(exp), checksum))
    print("data/rows.tsv rows=%d, data/expected.tsv groups=%d, checksum=%d"
          % (len(rows), len(exp), checksum))


if __name__ == "__main__":
    main()
