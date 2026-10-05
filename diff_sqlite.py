"""
diff_sqlite.py — 与参考实现 SQLite（Python 标准库 sqlite3）的对拍。

覆盖：
  1. 三值真值表（NOT/AND/OR 全组合，含 UNKNOWN）
  2. 空值比较（两边皆空 / 一边为空 / 空值与常量 / 整列全空）
  3. WHERE 过滤中 UNKNOWN 的传递
  4. JOIN ON 中 UNKNOWN 的传递
  5. 聚合（COUNT(*)/COUNT/SUM/AVG/MIN/MAX，含全 NULL 列与空输入）
  6. GROUP BY 中 NULL 同组
  7. DISTINCT 中 NULL 去重
  8. ORDER BY 中 NULL 位置（NULLS FIRST / NULLS LAST）
  9. 短路 / 求值顺序：同一随机谓词用 3 种策略求值，并与 SQLite 结果对照

运行：python3 diff_sqlite.py [--cases N] [--seed S]
"""

import argparse
import random
import sqlite3
import sys

from nullsem.tvl import (
    AND, IS_NOT_NULL, IS_NULL, NOT, NULL, OR, T, F, U,
    Col, Const, t_and, t_not, t_or,
)
from nullsem import engine

PASS = 0
FAIL = 0
FAILURES = []


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append((name, got, want))


def sqlite_truth(v):
    return {1: T, 0: F, None: U}[v]


def tri_str(t):
    return {T: "TRUE", F: "FALSE", U: "UNKNOWN"}[t]


# ---------------------------------------------------------------------------
# 随机谓词：同时生成 SQL 文本与库表达式树
# ---------------------------------------------------------------------------

class Gen:
    def __init__(self, rng, cols=("a", "b", "c")):
        self.rng = rng
        self.cols = cols

    def pred(self, depth):
        r = self.rng
        if depth == 0:
            k = r.randrange(4)
            if k == 0:
                col = r.choice(self.cols)
                op = r.choice(["=", "<>", "<", "<=", ">", ">="])
                c = r.randrange(5)
                return f"{col} {op} {c}", self._cmp(col, op, Const(c))
            if k == 1:
                c1, c2 = r.sample(self.cols, 2)
                op = r.choice(["=", "<>", "<", "<=", ">", ">="])
                return f"{c1} {op} {c2}", self._cmp(c1, op, Col(c2))
            if k == 2:
                col = r.choice(self.cols)
                return f"{col} IS NULL", IS_NULL(Col(col))
            col = r.choice(self.cols)
            return f"{col} IS NOT NULL", IS_NOT_NULL(Col(col))
        k = r.randrange(3)
        if k == 0:
            s1, e1 = self.pred(depth - 1)
            s2, e2 = self.pred(depth - 1)
            return f"({s1}) AND ({s2})", AND(e1, e2)
        if k == 1:
            s1, e1 = self.pred(depth - 1)
            s2, e2 = self.pred(depth - 1)
            return f"({s1}) OR ({s2})", OR(e1, e2)
        s, e = self.pred(depth - 1)
        return f"NOT ({s})", NOT(e)

    @staticmethod
    def _cmp(c1, op, right):
        from nullsem.tvl import Cmp
        return Cmp(op, Col(c1), right)


def rand_row(rng):
    pool = [None, 0, 1, 2, 3, 4]
    return {"a": rng.choice(pool), "b": rng.choice(pool), "c": rng.choice(pool)}


# ---------------------------------------------------------------------------
# 对拍主体
# ---------------------------------------------------------------------------

def diff_truth_tables(cur):
    for sql, lib in (("1", T), ("0", F), ("NULL", U)):
        for neg in (False, True):
            q = f"SELECT {'NOT ' if neg else ''}{sql}"
            got = sqlite_truth(cur.execute(q).fetchone()[0])
            want = t_not(lib) if neg else lib
            check(f"truth {q}", got, want)
    for a_sql, a in (("1", T), ("0", F), ("NULL", U)):
        for b_sql, b in (("1", T), ("0", F), ("NULL", U)):
            for op, fn in (("AND", t_and), ("OR", t_or)):
                q = f"SELECT {a_sql} {op} {b_sql}"
                got = sqlite_truth(cur.execute(q).fetchone()[0])
                check(f"truth {q}", got, fn(a, b))


def diff_null_compare(cur):
    vals = [("NULL", None), ("NULL", NULL), ("0", 0), ("1", 1), ("'x'", "x"), ("3.5", 3.5)]
    from nullsem.tvl import compare
    for sql_a, a in vals:
        for sql_b, b in vals:
            for op in ("=", "<>", "<", "<=", ">", ">="):
                # SQLite 与 Python 对 文本/数字混合比较规则不同，跳过混合类型
                if isinstance(a, str) != isinstance(b, str) and a is not None and b is not None \
                        and not isinstance(a, NULL.__class__) and not isinstance(b, NULL.__class__):
                    continue
                q = f"SELECT {sql_a} {op} {sql_b}"
                got = sqlite_truth(cur.execute(q).fetchone()[0])
                want = compare(op, a, b)
                check(f"cmp {q}", got, want)


def diff_predicates(cur, rng, n_cases):
    gen = Gen(rng)
    for i in range(n_cases):
        sql, expr = gen.pred(depth=3)
        rows = [rand_row(rng) for _ in range(rng.randrange(1, 8))]
        cur.execute("CREATE TEMP TABLE t (a, b, c)")
        cur.executemany("INSERT INTO t VALUES (?,?,?)",
                        [(r["a"], r["b"], r["c"]) for r in rows])
        # 逐行谓词真值对照
        got_vals = [sqlite_truth(v[0]) for v in cur.execute(f"SELECT {sql} FROM t")]
        want_vals = [expr.eval(r) for r in rows]
        check(f"pred[{i}] {sql} values", got_vals, want_vals)
        # WHERE 过滤对照
        got_ids = [tuple(x) for x in cur.execute(
            f"SELECT rowid FROM t WHERE {sql} ORDER BY rowid")]
        want_ids = [(i + 1,) for i, r in enumerate(rows)
                    if expr.eval(r) is T]
        check(f"pred[{i}] {sql} where", got_ids, want_ids)
        # 短路 / 求值顺序对拍：三种策略必须一致，且等于 SQLite
        from nullsem.tvl import And, Or
        for r, gv in zip(rows, got_vals):
            results = set()
            for sc in (True, False):
                for rev in (False, True):
                    results.add(_eval(expr, r, sc, rev))
            check(f"pred[{i}] {sql} strategies", len(results), 1)
            check(f"pred[{i}] {sql} vs sqlite", results.pop(), gv)
        cur.execute("DROP TABLE t")


def _eval(expr, row, sc, rev):
    from nullsem.tvl import And, Or
    if isinstance(expr, (And, Or)):
        return expr.eval(row, short_circuit=sc, reverse=rev)
    return expr.eval(row)


def diff_join(cur, rng, n_cases):
    for i in range(n_cases):
        left = [{"k": rng.choice([None, 1, 2, 3]), "v": rng.randrange(10)}
                for _ in range(rng.randrange(0, 6))]
        right = [{"k": rng.choice([None, 1, 2, 3]), "w": rng.randrange(10)}
                 for _ in range(rng.randrange(0, 6))]
        cur.execute("CREATE TEMP TABLE l (k, v)")
        cur.execute("CREATE TEMP TABLE r (k, w)")
        cur.executemany("INSERT INTO l VALUES (?,?)", [(x["k"], x["v"]) for x in left])
        cur.executemany("INSERT INTO r VALUES (?,?)", [(x["k"], x["w"]) for x in right])
        got = sorted(cur.execute(
            "SELECT l.k, l.v, r.k, r.w FROM l JOIN r ON l.k = r.k").fetchall())
        want_rows = engine.inner_join(left, right,
                                      __import__("nullsem.tvl", fromlist=["eq"]).eq(
                                          Col("l_k"), Col("r_k")),
                                      lprefix="l_", rprefix="r_")
        want = sorted((r["l_k"], r["l_v"], r["r_k"], r["r_w"]) for r in want_rows)
        check(f"join[{i}]", got, want)
        cur.execute("DROP TABLE l")
        cur.execute("DROP TABLE r")


def diff_aggregates(cur, rng, n_cases):
    for i in range(n_cases):
        if i % 7 == 0:  # 整列全空
            rows = [{"x": None} for _ in range(rng.randrange(0, 5))]
        else:
            rows = [{"x": rng.choice([None, 1, 2, 3, 10, -4])}
                    for _ in range(rng.randrange(0, 12))]
        cur.execute("CREATE TEMP TABLE t (x)")
        cur.executemany("INSERT INTO t VALUES (?)", [(r["x"],) for r in rows])
        got = cur.execute(
            "SELECT COUNT(*), COUNT(x), SUM(x), AVG(x), MIN(x), MAX(x) FROM t"
        ).fetchone()
        want = (
            engine.agg_count_star(rows),
            engine.agg_count(rows, "x"),
            _norm(engine.agg_sum(rows, "x")),
            _norm(engine.agg_avg(rows, "x")),
            _norm(engine.agg_min(rows, "x")),
            _norm(engine.agg_max(rows, "x")),
        )
        got = tuple(_norm(v) for v in got)
        check(f"agg[{i}] rows={rows}", got, want)
        cur.execute("DROP TABLE t")


def _norm(v):
    return None if v is NULL else v


def diff_group_by(cur, rng, n_cases):
    for i in range(n_cases):
        rows = [{"g": rng.choice([None, "a", "b"]), "v": rng.choice([None, 1, 2, 3])}
                for _ in range(rng.randrange(0, 15))]
        cur.execute("CREATE TEMP TABLE t (g, v)")
        cur.executemany("INSERT INTO t VALUES (?,?)", [(r["g"], r["v"]) for r in rows])
        got = sorted(cur.execute(
            "SELECT g, COUNT(*), COUNT(v), SUM(v) FROM t GROUP BY g").fetchall(),
            key=lambda r: (r[0] is not None, r[0]))
        want_rows = engine.group_by(
            rows, ["g"],
            {"n": engine.agg_count_star,
             "c": lambda rs: engine.agg_count(rs, "v"),
             "s": lambda rs: engine.agg_sum(rs, "v")})
        want = sorted(
            ((_norm(r["g"]), r["n"], r["c"], _norm(r["s"])) for r in want_rows),
            key=lambda r: (r[0] is not None, r[0]))
        check(f"group[{i}]", got, want)
        cur.execute("DROP TABLE t")


def diff_distinct(cur, rng, n_cases):
    for i in range(n_cases):
        rows = [{"x": rng.choice([None, 1, 2, 3])}
                for _ in range(rng.randrange(0, 12))]
        cur.execute("CREATE TEMP TABLE t (x)")
        cur.executemany("INSERT INTO t VALUES (?)", [(r["x"],) for r in rows])
        got = sorted(cur.execute("SELECT DISTINCT x FROM t").fetchall(),
                     key=lambda r: (r[0] is not None, r[0]))
        want = sorted(((_norm(r["x"]),) for r in engine.distinct(rows, ["x"])),
                      key=lambda r: (r[0] is not None, r[0]))
        check(f"distinct[{i}]", got, want)
        cur.execute("DROP TABLE t")


def diff_order_by(cur, rng, n_cases):
    for i in range(n_cases):
        rows = [{"id": j, "x": rng.choice([None, 1, 2, 3])}
                for j in range(rng.randrange(1, 12))]
        cur.execute("CREATE TEMP TABLE t (id, x)")
        cur.executemany("INSERT INTO t VALUES (?,?)", [(r["id"], r["x"]) for r in rows])
        for asc in (True, False):
            for nf in (True, False):
                q = (f"SELECT id, x FROM t ORDER BY x {'ASC' if asc else 'DESC'} "
                     f"{'NULLS FIRST' if nf else 'NULLS LAST'}, id")
                got = [(r[0], r[1]) for r in cur.execute(q)]
                want_rows = engine.order_by(rows, "x", ascending=asc, nulls_first=nf)
                # 引擎对同键保持稳定（输入序），SQL 侧用 ,id 打破并列，二者等价
                want = [(r["id"], _norm(r["x"])) for r in want_rows]
                check(f"order[{i}] asc={asc} nf={nf}", got, want)
        cur.execute("DROP TABLE t")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=300)
    ap.add_argument("--seed", type=int, default=20261005)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    con = sqlite3.connect(":memory:")
    cur = con.cursor()
    print(f"参考实现: SQLite {sqlite3.sqlite_version}")

    diff_truth_tables(cur)
    diff_null_compare(cur)
    diff_predicates(cur, rng, args.cases)
    diff_join(cur, rng, args.cases // 3)
    diff_aggregates(cur, rng, args.cases // 3)
    diff_group_by(cur, rng, args.cases // 3)
    diff_distinct(cur, rng, args.cases // 3)
    diff_order_by(cur, rng, args.cases // 3)

    print(f"对拍用例: PASS={PASS} FAIL={FAIL}")
    for name, got, want in FAILURES[:20]:
        print(f"  FAIL {name}\n    got  {got}\n    want {want}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
