"""Benchmark: 1,000,000 rows / 1,000,000 predicate evaluations.

Run: python3 bench.py
"""

import random
import time

import tvl
from tvl import NULL

N = 1_000_000


def timed(label, fn):
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    print("%-44s %8.3f s   (%s)" % (label, elapsed, result))
    return elapsed


def main():
    rng = random.Random(20261006)
    print("building %d rows ..." % N)
    rows = [
        (rng.choice((None, None, rng.randrange(100), rng.randrange(100))),
         rng.choice((None, None, rng.randrange(100), rng.randrange(100))))
        for _ in range(N)
    ]
    rows = [(NULL if a is None else a, NULL if b is None else b)
            for (a, b) in rows]
    col_a = [a for (a, _) in rows]
    print("NULL share in col a: %.1f%%" %
          (100.0 * sum(1 for v in col_a if v is NULL) / N))
    print()

    def eager_predicate():
        n_true = 0
        for (a, b) in rows:
            if tvl.and_(tvl.gt(a, 50), tvl.lt(b, 50)) is tvl.TRUE:
                n_true += 1
        return n_true

    def short_circuit_predicate():
        n_true = 0
        for (a, b) in rows:
            if tvl.and_sc(tvl.gt(a, 50), (lambda b=b: tvl.lt(b, 50))) is tvl.TRUE:
                n_true += 1
        return n_true

    def where_filter():
        return len(tvl.where(
            rows, lambda r: tvl.and_(tvl.gt(r[0], 50), tvl.lt(r[1], 50))))

    print("1,000,000 predicate evaluations per run:")
    timed("predicate (a>50) AND (b<50), eager 3VL", eager_predicate)
    timed("predicate (a>50) AND (b<50), short-circuit", short_circuit_predicate)
    timed("WHERE filter over 1,000,000 rows", where_filter)
    print()

    print("aggregates / set ops over 1,000,000 values:")
    timed("COUNT(a)", lambda: tvl.count(col_a))
    timed("SUM(a)", lambda: tvl.sum_(col_a))
    timed("AVG(a)", lambda: round(tvl.avg(col_a), 4))
    timed("MIN/MAX(a)",
          lambda: (tvl.min_(col_a), tvl.max_(col_a)))
    timed("GROUP BY a", lambda: len(tvl.group_by(rows, lambda r: r[0])))
    timed("DISTINCT a", lambda: len(tvl.distinct(col_a)))
    timed("ORDER BY a (nulls last)",
          lambda: len(tvl.order_by(col_a)))


if __name__ == "__main__":
    main()
