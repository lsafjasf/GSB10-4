"""单元测试：同类型 / 可提升 / 不可比 / 含空值 / 精度报告。"""
import math
import os
import random
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setops import (
    Dataset,
    IncomparableTypesError,
    Type,
    ValueConversionError,
    is_potentially_lossy,
    join,
)


class TestJoinTable(unittest.TestCase):
    def test_join_commutative_and_associative(self):
        types = list(Type)
        for a in types:
            for b in types:
                self.assertEqual(join(a, b), join(b, a), f"{a} vs {b}")
        # 结合律（NULL 参与的格运算）
        for a in types:
            for b in types:
                for c in types:
                    ab = join(a, b)
                    bc = join(b, c)
                    left = join(ab, c) if ab else None
                    right = join(a, bc) if bc else None
                    self.assertEqual(left, right, f"{a},{b},{c}")

    def test_specific_rules(self):
        self.assertIs(join(Type.NULL, Type.STRING), Type.STRING)
        self.assertIs(join(Type.BOOL, Type.INT32), Type.INT32)
        self.assertIs(join(Type.INT32, Type.FLOAT32), Type.FLOAT64)
        self.assertIs(join(Type.BOOL, Type.FLOAT32), Type.FLOAT32)
        self.assertIs(join(Type.INT64, Type.FLOAT64), Type.FLOAT64)
        self.assertIs(join(Type.DECIMAL, Type.BIGINT), Type.DECIMAL)
        self.assertIs(join(Type.DECIMAL, Type.FLOAT64), Type.FLOAT64)
        self.assertIs(join(Type.DATE, Type.DATETIME), Type.DATETIME)
        self.assertIsNone(join(Type.STRING, Type.INT64))
        self.assertIsNone(join(Type.BYTES, Type.STRING))
        self.assertTrue(is_potentially_lossy(Type.INT64, Type.FLOAT64))
        self.assertTrue(is_potentially_lossy(Type.DECIMAL, Type.FLOAT32))
        self.assertFalse(is_potentially_lossy(Type.INT32, Type.FLOAT64))


class TestSameType(unittest.TestCase):
    def test_int_union_intersection_difference(self):
        a = Dataset([Type.INT32], [(1,), (2,), (2,), (3,)])
        b = Dataset([Type.INT32], [(2,), (4,)])
        self.assertEqual(a.union(b).key_set(), {(1,), (2,), (3,), (4,)})
        self.assertEqual(a.intersection(b).key_set(), {(2,)})
        self.assertEqual(a.difference(b).key_set(), {(1,), (3,)})
        self.assertEqual(a.union(b).types, (Type.INT32,))

    def test_string_and_bytes(self):
        a = Dataset([Type.STRING], [("x",), ("y",)])
        b = Dataset([Type.STRING], [("y",), ("z",)])
        self.assertEqual(a.union(b).key_set(), {("x",), ("y",), ("z",)})
        c = Dataset([Type.BYTES], [(b"a",), (b"b",)])
        d = Dataset([Type.BYTES], [(b"b",)])
        self.assertEqual(c.difference(d).key_set(), {(b"a",)})


class TestPromotable(unittest.TestCase):
    def test_int32_float64(self):
        a = Dataset([Type.INT32], [(1,), (2,)])
        b = Dataset([Type.FLOAT64], [(2.0,), (2.5,)])
        r = a.union(b)
        self.assertEqual(r.types, (Type.FLOAT64,))
        self.assertEqual(r.key_set(), {(1.0,), (2.0,), (2.5,)})
        self.assertFalse(r.precision)  # int32 -> float64 精确

    def test_bool_int(self):
        a = Dataset([Type.BOOL], [(True,), (False,)])
        b = Dataset([Type.INT32], [(1,), (0,), (2,)])
        r = a.union(b)
        self.assertEqual(r.types, (Type.INT32,))
        self.assertEqual(r.key_set(), {(0,), (1,), (2,)})

    def test_decimal_int_exact(self):
        a = Dataset([Type.DECIMAL], [(Decimal("0.1"),)])
        b = Dataset([Type.BIGINT], [(2 ** 70,)])
        r = a.union(b)
        self.assertEqual(r.types, (Type.DECIMAL,))
        self.assertIn((Decimal(2 ** 70),), r.key_set())
        self.assertFalse(r.precision)

    def test_date_datetime(self):
        a = Dataset([Type.DATE], [(date(2024, 5, 1),)])
        b = Dataset([Type.DATETIME], [(datetime(2024, 5, 1, 0, 0),),
                                      (datetime(2024, 5, 1, 12, 0),)])
        r = a.union(b)
        self.assertEqual(r.types, (Type.DATETIME,))
        self.assertEqual(len(r.rows), 2)  # date 午夜与 datetime 午夜去重


class TestIncomparable(unittest.TestCase):
    def test_rejected_with_column_position(self):
        a = Dataset([Type.INT64, Type.STRING], [(1, "a")])
        b = Dataset([Type.BIGINT, Type.INT64], [(2, 3)])
        with self.assertRaises(IncomparableTypesError) as ctx:
            a.union(b)
        self.assertEqual(ctx.exception.column, 1)
        self.assertIn("列 1", str(ctx.exception))
        self.assertIn("string", str(ctx.exception))

    def test_no_silent_string_coercion(self):
        a = Dataset([Type.STRING], [("1",)])
        b = Dataset([Type.INT32], [(1,)])
        for op in ("union", "intersection", "difference"):
            with self.assertRaises(IncomparableTypesError):
                getattr(a, op)(b)

    def test_value_type_mismatch_position(self):
        with self.assertRaises(ValueConversionError) as ctx:
            Dataset([Type.INT32], [(1,), ("oops",)])
        self.assertEqual((ctx.exception.row, ctx.exception.column), (1, 0))


class TestNull(unittest.TestCase):
    def test_null_column_promotes(self):
        a = Dataset([Type.NULL], [(None,), (None,)])
        b = Dataset([Type.INT32], [(1,), (None,)])
        r = a.union(b)
        self.assertEqual(r.types, (Type.INT32,))
        self.assertEqual(r.key_set(), {(None,), (1,)})

    def test_null_dedup_and_equality(self):
        a = Dataset([Type.STRING], [("a",), (None,), (None,)])
        b = Dataset([Type.STRING], [(None,), ("b",)])
        self.assertEqual(a.intersection(b).key_set(), {(None,)})
        self.assertEqual(len(a.union(b).rows), 3)

    def test_nan_treated_as_single_value(self):
        nan = float("nan")
        a = Dataset([Type.FLOAT64], [(nan,), (nan,), (1.0,)])
        b = Dataset([Type.FLOAT64], [(float("nan"),)])
        self.assertEqual(len(a.union(b).rows), 2)
        self.assertEqual(len(a.intersection(b).rows), 1)


class TestPrecisionReport(unittest.TestCase):
    def test_int64_float64_loss_reported(self):
        big = 2 ** 53 + 1
        a = Dataset([Type.INT64], [(big,), (100,)])
        b = Dataset([Type.FLOAT64], [(0.5,)])
        r = a.union(b)
        self.assertEqual(r.precision.total_loss_count, 1)
        entry = r.precision.entries[0]
        self.assertEqual((entry.column, entry.src, entry.dst),
                         (0, Type.INT64, Type.FLOAT64))
        row, original, converted = entry.examples[0]
        self.assertEqual((row, original), (0, big))
        self.assertEqual(converted, float(big))  # 2^53
        self.assertIn((float(big),), r.key_set())

    def test_bigint_overflow_to_inf(self):
        a = Dataset([Type.BIGINT], [(10 ** 400,)])
        b = Dataset([Type.FLOAT64], [(1.0,)])
        r = a.union(b)
        self.assertEqual(r.precision.total_loss_count, 1)
        self.assertIn((math.inf,), r.key_set())

    def test_decimal_loss_reported(self):
        a = Dataset([Type.DECIMAL], [(Decimal("0.1"),), (Decimal("0.5"),)])
        b = Dataset([Type.FLOAT64], [(1.0,)])
        r = a.union(b)
        # 0.1 不能被 float64 精确表示 -> 报告；0.5 精确 -> 不报告
        self.assertEqual(r.precision.total_loss_count, 1)

    def test_exact_combos_silent(self):
        a = Dataset([Type.INT32], [(2 ** 31 - 1,)])
        b = Dataset([Type.FLOAT64], [(1.0,)])
        self.assertFalse(a.union(b).precision)


class TestOrderIndependence(unittest.TestCase):
    def test_shuffled_rows_same_result(self):
        from tools.pools import sample_row
        rng = random.Random(7)
        lt, rt = [Type.INT64, Type.STRING], [Type.FLOAT64, Type.STRING]
        left = [sample_row(lt, rng, 0.1) for _ in range(300)]
        right = [sample_row(rt, rng, 0.1) for _ in range(300)]
        base = Dataset(lt, left).union(Dataset(rt, right))
        for seed in range(5):
            r = random.Random(seed)
            l2, r2 = left[:], right[:]
            r.shuffle(l2)
            r.shuffle(r2)
            cur = Dataset(lt, l2).union(Dataset(rt, r2))
            self.assertEqual(base.key_set(), cur.key_set())
            self.assertEqual(base.precision.total_loss_count,
                             cur.precision.total_loss_count)

    def test_operand_swap_commutative_ops(self):
        a = Dataset([Type.INT64], [(2 ** 53 + 1,), (1,)])
        b = Dataset([Type.FLOAT64], [(1.0,), (2.5,)])
        for op in ("union", "intersection"):
            r1 = getattr(a, op)(b)
            r2 = getattr(b, op)(a)
            self.assertEqual(r1.types, r2.types)
            self.assertEqual(r1.key_set(), r2.key_set())
            self.assertEqual(r1.precision.total_loss_count,
                             r2.precision.total_loss_count)


if __name__ == "__main__":
    unittest.main()
