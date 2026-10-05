"""
test_nullsem.py — 空值语义与三值逻辑自测（unittest，标准库）。

运行：python3 -m unittest discover -s tests -v
"""

import itertools
import random
import unittest

from nullsem.tvl import (
    AND, IS_NOT_NULL, IS_NULL, NOT, NULL, OR, T, F, U, Tri,
    Col, Const, compare, eq, ge, gt, is_null, le, lt, ne,
    t_and, t_not, t_or,
)
from nullsem import engine


class TestTruthTables(unittest.TestCase):
    """真值表写全：NOT 3 项、AND/OR 各 9 项。"""

    def test_not_table(self):
        self.assertEqual(t_not(T), F)
        self.assertEqual(t_not(F), T)
        self.assertEqual(t_not(U), U)

    def test_and_table(self):
        expect = {
            (T, T): T, (T, F): F, (T, U): U,
            (F, T): F, (F, F): F, (F, U): F,
            (U, T): U, (U, F): F, (U, U): U,
        }
        for (a, b), want in expect.items():
            self.assertEqual(t_and(a, b), want, f"{a} AND {b}")

    def test_or_table(self):
        expect = {
            (T, T): T, (T, F): T, (T, U): T,
            (F, T): T, (F, F): F, (F, U): U,
            (U, T): T, (U, F): U, (U, U): U,
        }
        for (a, b), want in expect.items():
            self.assertEqual(t_or(a, b), want, f"{a} OR {b}")

    def test_de_morgan(self):
        for a, b in itertools.product((T, F, U), repeat=2):
            self.assertEqual(t_not(t_and(a, b)), t_or(t_not(a), t_not(b)))
            self.assertEqual(t_not(t_or(a, b)), t_and(t_not(a), t_not(b)))

    def test_and_or_commutative(self):
        for a, b in itertools.product((T, F, U), repeat=2):
            self.assertEqual(t_and(a, b), t_and(b, a))
            self.assertEqual(t_or(a, b), t_or(b, a))


class TestNullComparison(unittest.TestCase):
    """与空值的比较必须返回 UNKNOWN。覆盖两边皆空/一边为空/空值与常量。"""

    OPS = ("=", "<>", "!=", "<", "<=", ">", ">=")

    def test_both_null(self):
        for op in self.OPS:
            self.assertIs(compare(op, NULL, NULL), U, f"NULL {op} NULL")
            self.assertIs(compare(op, None, None), U)

    def test_one_side_null(self):
        for op in self.OPS:
            self.assertIs(compare(op, NULL, 5), U)
            self.assertIs(compare(op, 5, NULL), U)
            self.assertIs(compare(op, NULL, "x"), U)
            self.assertIs(compare(op, "x", None), U)

    def test_null_vs_constant_via_expr(self):
        row = {"a": NULL}
        for mk in (eq, ne, lt, le, gt, ge):
            self.assertIs(mk(Col("a"), Const(0)).eval(row), U)
            self.assertIs(mk(Const(0), Col("a")).eval(row), U)

    def test_no_null_normal_compare(self):
        self.assertIs(compare("=", 1, 1), T)
        self.assertIs(compare("=", 1, 2), F)
        self.assertIs(compare("<", 1, 2), T)
        self.assertIs(compare(">=", 1, 2), F)

    def test_is_null_never_unknown(self):
        self.assertIs(IS_NULL(Col("a")).eval({"a": NULL}), T)
        self.assertIs(IS_NULL(Col("a")).eval({"a": 1}), F)
        self.assertIs(IS_NOT_NULL(Col("a")).eval({"a": NULL}), F)
        self.assertIs(IS_NOT_NULL(Col("a")).eval({"a": 1}), T)

    def test_null_is_not_false(self):
        # UNKNOWN 不是 FALSE：NOT(UNKNOWN)=UNKNOWN；若把空值比较当假，这里会得 TRUE
        pred = NOT(eq(Col("a"), Const(1)))
        self.assertIs(pred.eval({"a": NULL}), U)
        self.assertIs(pred.eval({"a": 2}), T)


class TestPropagation(unittest.TestCase):
    """UNKNOWN 在过滤、连接、聚合中的传递规则逐一验证。"""

    ROWS = [
        {"id": 1, "a": 10, "b": 1},
        {"id": 2, "a": NULL, "b": 2},
        {"id": 3, "a": 30, "b": NULL},
        {"id": 4, "a": NULL, "b": NULL},
    ]

    def test_where_drops_unknown_and_false(self):
        pred = gt(Col("a"), Const(15))
        got = [r["id"] for r in engine.where(self.ROWS, pred)]
        self.assertEqual(got, [3])  # a=NULL 的 UNKNOWN 与 a=10 的 FALSE 都剔除

    def test_where_not_unknown_still_dropped(self):
        # 若实现把 UNKNOWN 当 FALSE，NOT 后会被错误保留
        pred = NOT(gt(Col("a"), Const(15)))
        got = [r["id"] for r in engine.where(self.ROWS, pred)]
        self.assertEqual(got, [1])

    def test_where_is_null(self):
        got = [r["id"] for r in engine.where(self.ROWS, IS_NULL(Col("a")))]
        self.assertEqual(got, [2, 4])

    def test_join_on_unknown_no_match(self):
        left = [{"k": 1}, {"k": NULL}, {"k": 3}]
        right = [{"k": 1}, {"k": NULL}, {"k": 3}]
        out = engine.inner_join(left, right, eq(Col("l_k"), Col("r_k")),
                                lprefix="l_", rprefix="r_")
        self.assertEqual(len(out), 2)  # NULL=NULL 是 UNKNOWN，不连接
        self.assertTrue(all(not is_null(r["l_k"]) for r in out))

    def test_join_with_constant_condition(self):
        left = [{"k": 1, "v": 5}, {"k": 2, "v": NULL}]
        right = [{"k": 1}, {"k": 2}]
        out = engine.inner_join(left, right,
                                AND(eq(Col("l_k"), Col("r_k")), gt(Col("l_v"), Const(0))),
                                lprefix="l_", rprefix="r_")
        self.assertEqual(len(out), 1)  # v=NULL -> UNKNOWN 不连接

    def test_aggregate_skips_null(self):
        rows = self.ROWS
        self.assertEqual(engine.agg_count_star(rows), 4)
        self.assertEqual(engine.agg_count(rows, "a"), 2)
        self.assertEqual(engine.agg_sum(rows, "a"), 40)
        self.assertAlmostEqual(engine.agg_avg(rows, "a"), 20.0)
        self.assertEqual(engine.agg_min(rows, "a"), 10)
        self.assertEqual(engine.agg_max(rows, "a"), 30)

    def test_aggregate_all_null_column(self):
        rows = [{"x": NULL}, {"x": None}, {"x": NULL}]
        self.assertEqual(engine.agg_count_star(rows), 3)
        self.assertEqual(engine.agg_count(rows, "x"), 0)
        self.assertIs(engine.agg_sum(rows, "x"), NULL)
        self.assertIs(engine.agg_avg(rows, "x"), NULL)
        self.assertIs(engine.agg_min(rows, "x"), NULL)
        self.assertIs(engine.agg_max(rows, "x"), NULL)

    def test_aggregate_empty_input(self):
        self.assertEqual(engine.agg_count_star([]), 0)
        self.assertIs(engine.agg_sum([], "x"), NULL)


class TestNullPlacement(unittest.TestCase):
    """空值在分组、去重、排序里的位置。"""

    def test_group_by_nulls_one_group(self):
        rows = [{"g": NULL, "v": 1}, {"g": None, "v": 2},
                {"g": "a", "v": 3}, {"g": "a", "v": 4}]
        out = engine.group_by(rows, ["g"], {"n": engine.agg_count_star,
                                            "s": lambda rs: engine.agg_sum(rs, "v")})
        by_g = {("NULL" if is_null(r["g"]) else r["g"]): r for r in out}
        self.assertEqual(len(out), 2)  # 两个 NULL 键同组
        self.assertEqual(by_g["NULL"]["n"], 2)
        self.assertEqual(by_g["NULL"]["s"], 3)
        self.assertEqual(by_g["a"]["n"], 2)

    def test_distinct_null_collapse(self):
        rows = [{"x": NULL}, {"x": None}, {"x": 1}, {"x": 1}, {"x": NULL}]
        out = engine.distinct(rows, ["x"])
        self.assertEqual(len(out), 2)
        self.assertEqual(sum(1 for r in out if is_null(r["x"])), 1)

    def test_order_by_null_positions(self):
        rows = [{"x": 3}, {"x": NULL}, {"x": 1}, {"x": None}, {"x": 2}]
        asc_last = [r["x"] for r in engine.order_by(rows, "x")]
        self.assertEqual(asc_last[:3], [1, 2, 3])
        self.assertTrue(all(is_null(v) for v in asc_last[3:]))
        asc_first = [r["x"] for r in engine.order_by(rows, "x", nulls_first=True)]
        self.assertTrue(all(is_null(v) for v in asc_first[:2]))
        self.assertEqual(asc_first[2:], [1, 2, 3])
        desc_last = [r["x"] for r in engine.order_by(rows, "x", ascending=False)]
        self.assertEqual(desc_last[:3], [3, 2, 1])
        self.assertTrue(all(is_null(v) for v in desc_last[3:]))
        desc_first = [r["x"] for r in engine.order_by(rows, "x",
                                                      ascending=False, nulls_first=True)]
        self.assertTrue(all(is_null(v) for v in desc_first[:2]))
        self.assertEqual(desc_first[2:], [3, 2, 1])


class TestShortCircuit(unittest.TestCase):
    """三值逻辑下短路与求值顺序不得改变结果（对拍）。"""

    def _random_pred(self, rng, depth):
        if depth == 0:
            kind = rng.randrange(3)
            if kind == 0:
                return gt(Col("a"), Const(rng.randrange(5)))
            if kind == 1:
                return eq(Col("b"), Const(rng.randrange(3)))
            return IS_NULL(Col("c"))
        op = rng.randrange(3)
        if op == 0:
            return AND(self._random_pred(rng, depth - 1),
                       self._random_pred(rng, depth - 1))
        if op == 1:
            return OR(self._random_pred(rng, depth - 1),
                      self._random_pred(rng, depth - 1))
        return NOT(self._random_pred(rng, depth - 1))

    def test_strategies_agree(self):
        rng = random.Random(20261005)
        for _ in range(2000):
            pred = self._random_pred(rng, depth=3)
            row = {
                "a": rng.choice([None, 0, 1, 2, 3, 4]),
                "b": rng.choice([None, 0, 1, 2]),
                "c": rng.choice([None, 7]),
            }
            results = set()
            for sc in (True, False):
                for rev in (False, True):
                    results.add(self._eval_all(pred, row, sc, rev))
            self.assertEqual(len(results), 1,
                             f"求值策略结果不一致: {results} row={row}")

    def _eval_all(self, expr, row, sc, rev):
        from nullsem.tvl import And, Or
        if isinstance(expr, (And, Or)):
            return expr.eval(row, short_circuit=sc, reverse=rev)
        return expr.eval(row)

    def test_and_or_table_vs_shortcut(self):
        # 直接对拍：And/Or 表达式三种策略 == 真值表
        for a, b in itertools.product((T, F, U), repeat=2):
            ea = AND(Const(1) if a is T else Const(0), Const(1) if b is T else Const(0))
            # 用 IS NULL 构造 UNKNOWN：NULL 与常量比较
            pa = self._const_pred(a)
            pb = self._const_pred(b)
            for sc in (True, False):
                for rev in (False, True):
                    self.assertIs(AND(pa, pb).eval({}, short_circuit=sc, reverse=rev),
                                  t_and(a, b))
                    self.assertIs(OR(pa, pb).eval({}, short_circuit=sc, reverse=rev),
                                  t_or(a, b))

    @staticmethod
    def _const_pred(tri):
        if tri is T:
            return eq(Const(1), Const(1))
        if tri is F:
            return eq(Const(1), Const(2))
        return eq(Const(NULL), Const(1))  # UNKNOWN


class TestEdgeCases(unittest.TestCase):
    """边界用例：两边皆空、一边为空、整列全空、空值与常量。"""

    def test_both_null_equality_not_true(self):
        self.assertIsNot(compare("=", NULL, NULL), T)

    def test_all_null_column_filter(self):
        rows = [{"x": NULL} for _ in range(100)]
        self.assertEqual(engine.where(rows, IS_NULL(Col("x"))), rows)
        self.assertEqual(engine.where(rows, eq(Col("x"), Const(1))), [])
        self.assertEqual(engine.where(rows, NOT(eq(Col("x"), Const(1)))), [])

    def test_null_vs_every_constant_type(self):
        for c in (0, -1, 3.14, "", "abc", True):
            self.assertIs(compare("=", NULL, c), U)
            self.assertIs(compare("<>", NULL, c), U)

    def test_zero_and_empty_string_not_null(self):
        self.assertIs(compare("=", 0, 0), T)
        self.assertIs(compare("=", "", ""), T)
        self.assertFalse(is_null(0))
        self.assertFalse(is_null(""))


if __name__ == "__main__":
    unittest.main()
