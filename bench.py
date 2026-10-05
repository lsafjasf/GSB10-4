"""
bench.py — 性能测试：100 万行数据、100 万次谓词求值。

运行：python3 bench.py [--rows N]
"""

import argparse
import random
import time

from nullsem.tvl import AND, IS_NOT_NULL, NULL, OR, Col, Const, gt, lt, eq
from nullsem import engine


def timed(name, fn):
    t0 = time.perf_counter()
    result = fn()
    dt = time.perf_counter() - t0
    print(f"{name:<44} {dt:8.3f} s")
    return result, dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1_000_000)
    args = ap.parse_args()
    n = args.rows

    rng = random.Random(42)
    rows, _ = timed(f"生成 {n:,} 行（约 1/3 列含 NULL）", lambda: [
        {"a": rng.choice([None, 1, 2, 3, 4, 5, 6, 7, 8, 9]),
         "b": rng.choice([None, 0, 1]),
         "g": rng.choice([None, "x", "y", "z"])}
        for _ in range(n)
    ])

    # 复合谓词：(a < 5 AND b IS NOT NULL) OR a = 9
    pred = OR(AND(lt(Col("a"), Const(5)), IS_NOT_NULL(Col("b"))),
              eq(Col("a"), Const(9)))

    def eval_1m():
        cnt = 0
        ev = pred.eval
        for r in rows:
            if ev(r) is not None:
                cnt += 1
        return cnt

    timed(f"谓词求值 {n:,} 次（复合 AND/OR/IS NOT NULL）", eval_1m)

    # 短路开关 + 反向求值各一遍，验证耗时与结果一致
    from nullsem.tvl import And, Or
    def eval_strategies():
        counts = []
        for sc in (True, False):
            for rev in (False, True):
                cnt = 0
                for r in rows:
                    v = pred.eval(r, short_circuit=sc, reverse=rev)
                    if v is not None:
                        cnt += 1
                counts.append(cnt)
        return counts

    counts, _ = timed(f"谓词求值 {n:,} 次 x 4 种策略（短路/顺序对拍）",
                      eval_strategies)
    assert len(set(counts)) == 1, f"策略结果不一致: {counts}"
    print(f"{'  四种策略结果一致: ' + str(counts[0]):<44}")

    kept, _ = timed(f"WHERE 过滤 {n:,} 行",
                    lambda: engine.where(rows, pred))
    print(f"{'  过滤后行数: ' + str(len(kept)):<44}")

    timed(f"聚合 SUM/AVG/MIN/MAX/COUNT {n:,} 行", lambda: (
        engine.agg_count_star(rows), engine.agg_count(rows, "a"),
        engine.agg_sum(rows, "a"), engine.agg_avg(rows, "a"),
        engine.agg_min(rows, "a"), engine.agg_max(rows, "a")))

    gb, _ = timed(f"GROUP BY {n:,} 行（含 NULL 键）", lambda: engine.group_by(
        rows, ["g"], {"n": engine.agg_count_star,
                      "s": lambda rs: engine.agg_sum(rs, "a")}))
    print(f"{'  分组数: ' + str(len(gb))}（NULL 键同组）")

    dc, _ = timed(f"DISTINCT {n:,} 行", lambda: engine.distinct(rows, ["a", "b"]))
    print(f"{'  去重后行数: ' + str(len(dc)):<44}")

    timed(f"ORDER BY {n:,} 行（NULLS LAST）",
          lambda: engine.order_by(rows, "a"))

    # 100 万次纯三值比较（compare 调用）
    def cmp_1m():
        from nullsem.tvl import compare
        vals = [None, 1, 2, 3, NULL]
        acc = 0
        for i in range(n):
            if compare("<", vals[i % 5], vals[(i * 7) % 5]) is not None:
                acc += 1
        return acc

    timed(f"空值安全比较 compare() {n:,} 次", cmp_1m)


if __name__ == "__main__":
    main()
