"""基准：1000 种类型组合 × 每种 1000 行 = 1,000,000 行。

运行：python3 bench/benchmark.py
产物：reports/benchmark.md
"""

import os
import platform
import random
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from typeunify import union

N_COMBOS = 1000
ROWS_PER_COMBO = 1000  # 两个集合各 500 行
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "reports", "benchmark.md")

# 可兼容的类型组合池（生成器按种子确定性抽样，凑满 1000 种）
GEN_POOL = [
    ("int", lambda rng: rng.randint(-10 ** 6, 10 ** 6)),
    ("bool", lambda rng: rng.choice([True, False])),
    ("float", lambda rng: rng.uniform(-1e4, 1e4)),
    ("decimal", lambda rng: Decimal(rng.randint(-10 ** 6, 10 ** 6)) / 100),
    ("bigint", lambda rng: rng.randint(2 ** 53, 2 ** 60)),          # 触发 INT->FLOAT 精度报告
    ("string", lambda rng: "s%d" % rng.randint(0, 10 ** 6)),
    ("null", lambda rng: None),
]
COMPATIBLE = [
    ("int", "bool"), ("int", "float"), ("int", "decimal"),
    ("int", "float", "null"), ("int", "decimal", "null"),
    ("bool", "float"), ("bool", "decimal"),
    ("float", "decimal"), ("float", "decimal", "null"),
    ("bigint", "float"), ("bigint", "float", "null"),
    ("string", "null"), ("int", "null"), ("float", "null"),
    ("int", "bool", "float"), ("int", "bool", "decimal"),
    ("int", "float", "decimal"), ("bool", "int", "float", "null"),
]


def gen_value(kind, rng):
    return dict(GEN_POOL)[kind](rng)


def main():
    rng = random.Random(20261007)
    combos = [COMPATIBLE[i % len(COMPATIBLE)] for i in range(N_COMBOS)]
    rng.shuffle(combos)

    total_rows = 0
    total_loss = 0
    t0 = time.perf_counter()
    for combo in combos:
        half = ROWS_PER_COMBO // 2
        col_a = [gen_value(combo[i % len(combo)], rng) for i in range(half)]
        col_b = [gen_value(combo[(i + 1) % len(combo)], rng) for i in range(half)]
        r = union(col_a, col_b, max_samples=0)
        total_rows += half * 2
        total_loss += r.precision.total_loss
    elapsed = time.perf_counter() - t0

    assert total_rows == N_COMBOS * ROWS_PER_COMBO == 1_000_000
    rate = total_rows / elapsed
    lines = [
        "# 基准结果（1,000,000 行 × 1,000 种类型组合）",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        "| 总行数 | %d |" % total_rows,
        "| 类型组合数 | %d |" % N_COMBOS,
        "| 每组合行数 | %d |" % ROWS_PER_COMBO,
        "| 总耗时 | %.3f s |" % elapsed,
        "| 吞吐 | %.0f 行/s |" % rate,
        "| 平均每行 | %.2f µs |" % (elapsed / total_rows * 1e6),
        "| 精度丢失事件 | %d |" % total_loss,
        "| Python | %s |" % platform.python_version(),
        "| 平台 | %s |" % platform.platform(),
        "",
        "说明：每种组合执行一次两集合并集（含类型推断、统一、转换、去重、排序）；",
        "组合由固定种子（20261007）从兼容类型池确定性抽样，含 NULL 与触发精度报告的组合。",
    ]
    text = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
