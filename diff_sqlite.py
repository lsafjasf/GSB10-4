"""Differential test: tvl.py vs SQLite (stdlib sqlite3) as the reference
implementation of SQL NULL / three-valued-logic semantics.

Run: python3 diff_sqlite.py [n_rows]
Exits 0 when every check matches the reference, 1 otherwise.
"""

import random
import sqlite3
import sys

import tvl
from tvl import TRUE, FALSE, UNKNOWN, NULL

# Map SQLite result (0/1/None) to a 3VL truth value.
def ref_tri(v):
    return {0: FALSE, 1: TRUE, None: UNKNOWN}[v]


FAILURES = []
CHECKS = [0]


def check(label, got, want):
    CHECKS[0] += 1
    if got != want:
        FAILURES.append((label, got, want))


def to_lib(v):
    return NULL if v is None else v


def run(n_rows, seed=20261006):
    rng = random.Random(seed)
    domain = [None, 0, 1, 2, 3, 5, 7, 10, 42, 100]
    rows = [(rng.choice(domain), rng.choice(domain)) for _ in range(n_rows)]
    # Force the required edge cases into the data set.
    rows += [(None, None), (None, 5), (5, None), (0, 0), (100, 100)]

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?)", rows)

    lib_rows = [(to_lib(a), to_lib(b)) for (a, b) in rows]

    # 1) Comparison operators, row by row.
    for op_sql, op_lib in (("=", "="), ("<>", "<>"), ("<", "<"),
                           ("<=", "<="), (">", ">"), (">=", ">=")):
        ref = [ref_tri(r[0]) for r in
               db.execute("SELECT a %s b FROM t" % op_sql)]
        got = [tvl.compare(op_lib, a, b) for (a, b) in lib_rows]
        check("compare %s" % op_sql, got, ref)

    # 2) AND / OR / NOT over comparisons (UNKNOWN propagation).
    exprs = [
        ("(a > 5) AND (b < 10)",
         lambda a, b: tvl.and_(tvl.gt(a, 5), tvl.lt(b, 10))),
        ("(a > 5) OR (b < 10)",
         lambda a, b: tvl.or_(tvl.gt(a, 5), tvl.lt(b, 10))),
        ("NOT (a = 5)",
         lambda a, b: tvl.not_(tvl.eq(a, 5))),
        ("(a = b) AND (a > 1) OR (b <= 3)",
         lambda a, b: tvl.or_(tvl.and_(tvl.eq(a, b), tvl.gt(a, 1)),
                              tvl.le(b, 3))),
    ]
    for sql, lib_fn in exprs:
        ref = [ref_tri(r[0]) for r in db.execute("SELECT %s FROM t" % sql)]
        got = [lib_fn(a, b) for (a, b) in lib_rows]
        check("expr %s" % sql, got, ref)

    # 3) WHERE keeps only TRUE.
    ref = db.execute("SELECT a, b FROM t WHERE (a > 5) AND (b < 10)").fetchall()
    got = tvl.where(lib_rows, lambda r: tvl.and_(tvl.gt(r[0], 5),
                                                 tvl.lt(r[1], 10)))
    check("where", [(a, b) for (a, b) in got],
          [(to_lib(a), to_lib(b)) for (a, b) in ref])

    # 4) INNER JOIN ON equality: NULL keys never match.
    left = [r for r in lib_rows[:60] if not tvl.is_null(r[1])]
    right = lib_rows[:60]
    got = tvl.inner_join(left, right, lambda l, r: tvl.eq(l[0], r[0]))
    got_pairs = sorted((repr(l[0]), repr(r[1])) for (l, r) in got)
    ref_pairs = sorted((repr(to_lib(a)), repr(to_lib(b))) for (a, b) in
                       db.execute(
                           "SELECT l.a, r.b FROM (SELECT a, b FROM t LIMIT 60) l "
                           "JOIN (SELECT a, b FROM t LIMIT 60) r ON l.a = r.a "
                           "WHERE l.b IS NOT NULL"))
    check("inner_join", got_pairs, ref_pairs)

    # 5) Aggregates skip NULL; all-NULL -> NULL (COUNT -> 0).
    for col_sql, col_lib, lib_fn in (
        ("COUNT(a)", None, tvl.count),
        ("SUM(a)", None, tvl.sum_),
        ("MIN(a)", None, tvl.min_),
        ("MAX(a)", None, tvl.max_),
    ):
        ref = db.execute("SELECT %s FROM t" % col_sql).fetchone()[0]
        got = lib_fn([a for (a, _) in lib_rows])
        got = None if got is NULL else got
        check("agg %s" % col_sql, got, ref)
    ref_avg = db.execute("SELECT AVG(a) FROM t").fetchone()[0]
    got_avg = tvl.avg([a for (a, _) in lib_rows])
    check("agg AVG(a)", abs(got_avg - ref_avg) < 1e-9, True)
    check("agg COUNT(*)", tvl.count_star(lib_rows),
          db.execute("SELECT COUNT(*) FROM t").fetchone()[0])

    # All-NULL column.
    db.execute("CREATE TABLE tn (x INTEGER)")
    db.executemany("INSERT INTO tn VALUES (NULL)", [()] * 50)
    null_col = [NULL] * 50
    check("all-null COUNT(x)", tvl.count(null_col),
          db.execute("SELECT COUNT(x) FROM tn").fetchone()[0])
    for name, lib_fn in (("SUM", tvl.sum_), ("AVG", tvl.avg),
                         ("MIN", tvl.min_), ("MAX", tvl.max_)):
        ref = db.execute("SELECT %s(x) FROM tn" % name).fetchone()[0]
        got = lib_fn(null_col)
        check("all-null %s" % name, None if got is NULL else got, ref)

    # 6) GROUP BY: NULLs form one group.
    ref = sorted(db.execute(
        "SELECT IFNULL(a, -999), COUNT(*) FROM t GROUP BY a"))
    groups = tvl.group_by(lib_rows, lambda r: r[0])
    got = sorted((-999 if k is NULL else k, len(v)) for k, v in groups.items())
    check("group_by", got, ref)

    # 7) DISTINCT: NULLs collapse to a single value.
    ref = sorted(db.execute("SELECT DISTINCT IFNULL(a, -999) FROM t"))
    got = sorted(-999 if v is NULL else v
                 for v in tvl.distinct([a for (a, _) in lib_rows]))
    check("distinct", [(v,) for v in got], ref)

    # 8) ORDER BY: SQLite ASC puts NULLs first.
    ref = [r[0] for r in db.execute("SELECT IFNULL(a, -999) FROM t ORDER BY a")]
    got = [-999 if v is NULL else v
           for v in tvl.order_by([a for (a, _) in lib_rows],
                                 nulls_first=True)]
    check("order_by asc nulls first", got, ref)
    ref = [r[0] for r in db.execute(
        "SELECT IFNULL(a, -999) FROM t ORDER BY a DESC")]
    got = [-999 if v is NULL else v
           for v in tvl.order_by([a for (a, _) in lib_rows],
                                 reverse=True, nulls_first=False)]
    check("order_by desc nulls last", got, ref)

    # 9) Short-circuit vs SQLite: (a > 5) AND (b < 10) with NULLs.
    ref = [ref_tri(r[0]) for r in
           db.execute("SELECT (a > 5) AND (b < 10) FROM t")]
    got = [tvl.and_sc(tvl.gt(a, 5), (lambda b=b: tvl.lt(b, 10)))
           for (a, b) in lib_rows]
    check("short-circuit AND vs sqlite", got, ref)
    ref = [ref_tri(r[0]) for r in
           db.execute("SELECT (a > 5) OR (b < 10) FROM t")]
    got = [tvl.or_sc(tvl.gt(a, 5), (lambda b=b: tvl.lt(b, 10)))
           for (a, b) in lib_rows]
    check("short-circuit OR vs sqlite", got, ref)

    db.close()
    return CHECKS[0], len(rows)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    checks, n_rows = run(n)
    if FAILURES:
        print("MISMATCHES: %d / %d checks" % (len(FAILURES), checks))
        for label, got, want in FAILURES[:10]:
            print("  %s: got %r want %r" % (label, got, want))
        return 1
    print("OK: %d differential checks vs sqlite3 reference passed "
          "(%d rows, seed=20261006)" % (checks, n_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
