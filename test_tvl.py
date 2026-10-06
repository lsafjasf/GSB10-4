"""Self-tests for tvl.py: truth tables, propagation rules, NULL placement,
short-circuit equivalence, and edge cases. Run: python3 test_tvl.py -v
"""

import unittest

from tvl import (
    TRUE, FALSE, UNKNOWN, NULL,
    and_, or_, not_, and_sc, or_sc,
    compare, eq, ne, lt, le, gt, ge, is_null_pred,
    where, inner_join, having,
    count, count_star, sum_, avg, min_, max_,
    group_by, distinct, order_by,
)

TRIS = (TRUE, FALSE, UNKNOWN)


class TestTruthTables(unittest.TestCase):
    """Full 3VL truth tables (Kleene strong logic, SQL semantics)."""

    def test_and_table(self):
        expected = {
            (TRUE, TRUE): TRUE, (TRUE, FALSE): FALSE, (TRUE, UNKNOWN): UNKNOWN,
            (FALSE, TRUE): FALSE, (FALSE, FALSE): FALSE, (FALSE, UNKNOWN): FALSE,
            (UNKNOWN, TRUE): UNKNOWN, (UNKNOWN, FALSE): FALSE,
            (UNKNOWN, UNKNOWN): UNKNOWN,
        }
        for a in TRIS:
            for b in TRIS:
                self.assertIs(and_(a, b), expected[(a, b)], "AND %s %s" % (a, b))

    def test_or_table(self):
        expected = {
            (TRUE, TRUE): TRUE, (TRUE, FALSE): TRUE, (TRUE, UNKNOWN): TRUE,
            (FALSE, TRUE): TRUE, (FALSE, FALSE): FALSE, (FALSE, UNKNOWN): UNKNOWN,
            (UNKNOWN, TRUE): TRUE, (UNKNOWN, FALSE): UNKNOWN,
            (UNKNOWN, UNKNOWN): UNKNOWN,
        }
        for a in TRIS:
            for b in TRIS:
                self.assertIs(or_(a, b), expected[(a, b)], "OR %s %s" % (a, b))

    def test_not_table(self):
        self.assertIs(not_(TRUE), FALSE)
        self.assertIs(not_(FALSE), TRUE)
        self.assertIs(not_(UNKNOWN), UNKNOWN)

    def test_and_or_commutative(self):
        for a in TRIS:
            for b in TRIS:
                self.assertIs(and_(a, b), and_(b, a))
                self.assertIs(or_(a, b), or_(b, a))


class TestNullComparison(unittest.TestCase):
    """Any comparison touching NULL must return UNKNOWN."""

    OPS = ("=", "<>", "<", "<=", ">", ">=")

    def test_both_null(self):
        for op in self.OPS:
            self.assertIs(compare(op, NULL, NULL), UNKNOWN, op)
            self.assertIs(compare(op, None, None), UNKNOWN, op)

    def test_one_null_left(self):
        for op in self.OPS:
            self.assertIs(compare(op, NULL, 5), UNKNOWN, op)

    def test_one_null_right(self):
        for op in self.OPS:
            self.assertIs(compare(op, 5, NULL), UNKNOWN, op)

    def test_null_vs_constant(self):
        for op in self.OPS:
            self.assertIs(compare(op, NULL, 0), UNKNOWN, op)
            self.assertIs(compare(op, NULL, ""), UNKNOWN, op)

    def test_null_not_equal_null(self):
        # NULL = NULL is UNKNOWN, not TRUE; NULL <> NULL is UNKNOWN, not FALSE.
        self.assertIs(eq(NULL, NULL), UNKNOWN)
        self.assertIs(ne(NULL, NULL), UNKNOWN)

    def test_non_null_comparisons(self):
        self.assertIs(eq(3, 3), TRUE)
        self.assertIs(eq(3, 4), FALSE)
        self.assertIs(ne(3, 4), TRUE)
        self.assertIs(lt(3, 4), TRUE)
        self.assertIs(le(4, 4), TRUE)
        self.assertIs(gt(4, 3), TRUE)
        self.assertIs(ge(3, 4), FALSE)

    def test_is_null_pred_never_unknown(self):
        self.assertIs(is_null_pred(NULL), TRUE)
        self.assertIs(is_null_pred(None), TRUE)
        self.assertIs(is_null_pred(0), FALSE)
        self.assertIs(is_null_pred(""), FALSE)


class TestUnknownPropagation(unittest.TestCase):
    """UNKNOWN propagation through join, filter, and aggregation."""

    def test_where_keeps_only_true(self):
        rows = [1, 2, None, 4, None, 10]
        kept = where(rows, lambda r: gt(r, 3))
        # 1->FALSE dropped, 2->FALSE dropped, None->UNKNOWN dropped,
        # 4->TRUE kept, None->UNKNOWN dropped, 10->TRUE kept
        self.assertEqual(kept, [4, 10])

    def test_where_unknown_is_not_true(self):
        rows = [None, None, None]
        self.assertEqual(where(rows, lambda r: eq(r, r)), [])

    def test_join_on_unknown_no_match(self):
        left = [(1, "a"), (2, "b"), (None, "c")]
        right = [(1, "x"), (2, "y"), (None, "z")]
        joined = inner_join(left, right, lambda l, r: eq(l[0], r[0]))
        # NULL=NULL -> UNKNOWN -> no match; only (1,x) and (2,y) survive
        self.assertEqual(joined, [((1, "a"), (1, "x")), ((2, "b"), (2, "y"))])

    def test_having_keeps_only_true(self):
        groups = {"g1": [1, 2], "g2": [None, None], "g3": [5]}
        kept = having(groups, lambda g: gt(count(g), 1))
        # g1: 2>1 TRUE kept; g2: 0>1 FALSE dropped; g3: 1>1 FALSE dropped
        self.assertEqual(list(kept), ["g1"])

    def test_aggregates_skip_null(self):
        col = [10, None, 20, None, 30]
        self.assertEqual(count(col), 3)
        self.assertEqual(sum_(col), 60)
        self.assertEqual(avg(col), 20.0)
        self.assertEqual(min_(col), 10)
        self.assertEqual(max_(col), 30)

    def test_aggregates_all_null_column(self):
        col = [None, NULL, None]
        self.assertEqual(count(col), 0)          # COUNT(col) -> 0
        self.assertIs(sum_(col), NULL)           # SUM -> NULL
        self.assertIs(avg(col), NULL)            # AVG -> NULL
        self.assertIs(min_(col), NULL)           # MIN -> NULL
        self.assertIs(max_(col), NULL)           # MAX -> NULL
        self.assertEqual(count_star(col), 3)     # COUNT(*) counts rows

    def test_aggregates_empty_input(self):
        self.assertEqual(count([]), 0)
        self.assertIs(sum_([]), NULL)
        self.assertIs(avg([]), NULL)

    def test_unknown_through_and_or(self):
        # UNKNOWN AND FALSE -> FALSE (row dropped), UNKNOWN AND TRUE -> UNKNOWN
        # (still dropped by WHERE): the two are indistinguishable in a filter,
        # but distinguishable under NOT / OR.
        self.assertIs(and_(UNKNOWN, FALSE), FALSE)
        self.assertIs(and_(UNKNOWN, TRUE), UNKNOWN)
        self.assertIs(or_(UNKNOWN, TRUE), TRUE)
        self.assertIs(or_(UNKNOWN, FALSE), UNKNOWN)


class TestNullPlacement(unittest.TestCase):
    """NULL in GROUP BY, DISTINCT, ORDER BY."""

    def test_group_by_nulls_one_group(self):
        rows = [("a", 1), (None, 2), ("a", 3), (NULL, 4), ("b", 5), (None, 6)]
        groups = group_by(rows, lambda r: r[0])
        self.assertEqual(set(groups), {"a", "b", NULL})
        # None and NULL collapse into the same group
        self.assertEqual(groups[NULL], [(None, 2), (NULL, 4), (None, 6)])

    def test_group_by_all_null(self):
        rows = [(None, 1), (None, 2), (None, 3)]
        groups = group_by(rows, lambda r: r[0])
        self.assertEqual(list(groups), [NULL])
        self.assertEqual(len(groups[NULL]), 3)

    def test_distinct_nulls_collapse(self):
        self.assertEqual(distinct([1, None, 2, NULL, None, 1, 2]), [1, NULL, 2])

    def test_distinct_all_null(self):
        self.assertEqual(distinct([None, NULL, None]), [NULL])

    def test_order_by_nulls_last_asc(self):
        self.assertEqual(order_by([3, None, 1, NULL, 2]), [1, 2, 3, NULL, NULL])

    def test_order_by_nulls_first_desc(self):
        self.assertEqual(
            order_by([3, None, 1, NULL, 2], reverse=True),
            [NULL, NULL, 3, 2, 1],
        )

    def test_order_by_explicit_placement(self):
        self.assertEqual(
            order_by([2, None, 1], nulls_first=True), [NULL, 1, 2])
        self.assertEqual(
            order_by([2, None, 1], reverse=True, nulls_first=False),
            [2, 1, NULL],
        )

    def test_order_by_all_null(self):
        self.assertEqual(order_by([None, None, None]), [NULL, NULL, NULL])


class TestShortCircuit(unittest.TestCase):
    """Short-circuit and evaluation order must not change results."""

    def test_and_sc_matches_table(self):
        for a in TRIS:
            for b in TRIS:
                self.assertIs(and_sc(a, lambda: b), and_(a, b))

    def test_or_sc_matches_table(self):
        for a in TRIS:
            for b in TRIS:
                self.assertIs(or_sc(a, lambda: b), or_(a, b))

    def test_and_sc_skips_rhs_only_on_false(self):
        calls = []
        def rhs(v):
            calls.append(v)
            return v
        and_sc(FALSE, lambda: rhs(TRUE))
        self.assertEqual(calls, [])                 # short-circuited
        and_sc(TRUE, lambda: rhs(TRUE))
        and_sc(UNKNOWN, lambda: rhs(FALSE))
        self.assertEqual(calls, [TRUE, FALSE])      # evaluated both times

    def test_or_sc_skips_rhs_only_on_true(self):
        calls = []
        def rhs(v):
            calls.append(v)
            return v
        or_sc(TRUE, lambda: rhs(FALSE))
        self.assertEqual(calls, [])                 # short-circuited
        or_sc(FALSE, lambda: rhs(TRUE))
        or_sc(UNKNOWN, lambda: rhs(TRUE))
        self.assertEqual(calls, [TRUE, TRUE])       # evaluated both times

    def test_evaluation_order_irrelevant(self):
        # Swapping operand order must not change the result for any combo.
        for a in TRIS:
            for b in TRIS:
                self.assertIs(and_sc(a, lambda: b), and_sc(b, lambda: a))
                self.assertIs(or_sc(a, lambda: b), or_sc(b, lambda: a))

    def test_short_circuit_vs_eager_random_predicates(self):
        # Differential data: eager vs short-circuit over random 3VL streams.
        import random
        rng = random.Random(20261006)
        for _ in range(100000):
            a = rng.choice(TRIS)
            b = rng.choice(TRIS)
            self.assertIs(and_sc(a, lambda: b), and_(a, b))
            self.assertIs(or_sc(a, lambda: b), or_(a, b))


class TestEdgeCases(unittest.TestCase):
    """Both-NULL, one-NULL, all-NULL column, NULL vs constant."""

    def test_both_sides_null_expression(self):
        # (NULL = NULL) AND (NULL = NULL) -> UNKNOWN AND UNKNOWN -> UNKNOWN
        self.assertIs(and_(eq(NULL, NULL), eq(NULL, NULL)), UNKNOWN)
        self.assertIs(or_(eq(NULL, NULL), eq(NULL, NULL)), UNKNOWN)
        self.assertIs(not_(eq(NULL, NULL)), UNKNOWN)

    def test_one_side_null_expression(self):
        # (NULL < 10) OR (5 = 5) -> UNKNOWN OR TRUE -> TRUE
        self.assertIs(or_(lt(NULL, 10), eq(5, 5)), TRUE)
        # (NULL < 10) AND (5 = 5) -> UNKNOWN AND TRUE -> UNKNOWN
        self.assertIs(and_(lt(NULL, 10), eq(5, 5)), UNKNOWN)

    def test_all_null_column_filtered_to_empty(self):
        rows = [{"v": None}, {"v": None}, {"v": None}]
        self.assertEqual(where(rows, lambda r: gt(r["v"], 0)), [])
        self.assertEqual(count([r["v"] for r in rows]), 0)
        self.assertIs(sum_([r["v"] for r in rows]), NULL)

    def test_null_vs_constant_in_filter(self):
        rows = [{"v": None}, {"v": 7}, {"v": 3}]
        kept = where(rows, lambda r: ge(r["v"], 5))
        self.assertEqual(kept, [{"v": 7}])

    def test_no_boolean_coercion(self):
        for v in (TRUE, FALSE, UNKNOWN, NULL):
            with self.assertRaises(TypeError):
                bool(v)


if __name__ == "__main__":
    unittest.main()
