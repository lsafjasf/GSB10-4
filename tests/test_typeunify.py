import random
import unittest
from decimal import Decimal

from typeunify import (
    LogicalType,
    TypeUnificationError,
    difference,
    intersection,
    join_types,
    union,
)


class TestSameType(unittest.TestCase):
    def test_int_union(self):
        r = union([1, 2, 2, 3], [3, 4])
        self.assertEqual(r.unified_type, LogicalType.INT)
        self.assertEqual(r.values, [1, 2, 3, 4])
        self.assertFalse(r.precision.has_loss)

    def test_string_intersection(self):
        r = intersection(["a", "b", "c"], ["b", "c", "d"])
        self.assertEqual(r.unified_type, LogicalType.STRING)
        self.assertEqual(r.values, ["b", "c"])

    def test_float_difference(self):
        r = difference([1.5, 2.5, 3.5], [2.5])
        self.assertEqual(r.unified_type, LogicalType.FLOAT)
        self.assertEqual(r.values, [1.5, 3.5])


class TestPromotable(unittest.TestCase):
    def test_bool_int(self):
        r = union([True, False], [2, 3])
        self.assertEqual(r.unified_type, LogicalType.INT)
        self.assertEqual(r.values, [0, 1, 2, 3])

    def test_int_float(self):
        r = union([1, 2], [2.5])
        self.assertEqual(r.unified_type, LogicalType.FLOAT)
        self.assertEqual(r.values, [1.0, 2.0, 2.5])

    def test_int_decimal_exact(self):
        r = union([1], [Decimal("1.5")])
        self.assertEqual(r.unified_type, LogicalType.DECIMAL)
        self.assertEqual(r.values, [Decimal(1), Decimal("1.5")])
        self.assertFalse(r.precision.has_loss)

    def test_float_decimal_promotes_to_float(self):
        r = union([0.5], [Decimal("1.5")])
        self.assertEqual(r.unified_type, LogicalType.FLOAT)
        self.assertEqual(r.values, [0.5, 1.5])

    def test_cross_type_equality_after_promotion(self):
        # 1(int) / True(bool) / 1.0(float) 提升后应视为同一元素
        r = intersection([1, 2], [True, 2.0])
        self.assertEqual(r.unified_type, LogicalType.FLOAT)
        self.assertEqual(r.values, [1.0, 2.0])


class TestNulls(unittest.TestCase):
    def test_null_with_int(self):
        r = union([None, 1], [2, None])
        self.assertEqual(r.unified_type, LogicalType.INT)
        self.assertEqual(r.values, [None, 1, 2])

    def test_all_null(self):
        r = union([None, None], [None])
        self.assertEqual(r.unified_type, LogicalType.NULL)
        self.assertEqual(r.values, [None])

    def test_null_with_string(self):
        r = union([None], ["x"])
        self.assertEqual(r.unified_type, LogicalType.STRING)
        self.assertEqual(r.values, [None, "x"])


class TestIncomparable(unittest.TestCase):
    def test_string_vs_int_rejected(self):
        with self.assertRaises(TypeUnificationError):
            union(["a"], [1])

    def test_bytes_vs_int_rejected(self):
        with self.assertRaises(TypeUnificationError):
            union([b"x"], [1])

    def test_error_reports_positions(self):
        try:
            union([1, 2, 3], ["x", "y"])
            self.fail("should raise")
        except TypeUnificationError as e:
            self.assertEqual(e.pos_a, (0, 0))   # INT first seen
            self.assertEqual(e.pos_b, (1, 0))   # STRING first seen
            self.assertIn("collection 1, row 0", str(e))

    def test_error_is_order_independent(self):
        msgs = set()
        for cols in [([1], ["a"], [b"b"]), ([b"b"], ["a"], [1]), (["a"], [1], [b"b"])]:
            with self.assertRaises(TypeUnificationError) as cm:
                union(*cols)
            e = cm.exception
            msgs.add((e.type_a, e.type_b))
        self.assertEqual(len(msgs), 1)  # 总是报同一对冲突类型


class TestOrderIndependence(unittest.TestCase):
    def test_shuffled_collections_and_rows(self):
        base = [[1, None, True], [2.5, 3], [Decimal("4.5"), 5, None]]
        ref = union(*base)
        rng = random.Random(20261007)
        for _ in range(200):
            shuffled = [list(c) for c in base]
            for c in shuffled:
                rng.shuffle(c)
            rng.shuffle(shuffled)
            r = union(*shuffled)
            self.assertEqual(r.unified_type, ref.unified_type)
            self.assertEqual(r.values, ref.values)
            self.assertEqual(r.precision.counts, ref.precision.counts)

    def test_join_associativity(self):
        types = list(LogicalType)
        for a in types:
            for b in types:
                for c in types:
                    ab = join_types(a, b)
                    bc = join_types(b, c)
                    left = None if ab is None else join_types(ab, c)
                    right = None if bc is None else join_types(a, bc)
                    self.assertEqual(left, right, (a, b, c))


class TestPrecision(unittest.TestCase):
    def test_big_int_to_float_reported(self):
        big = 2 ** 53 + 1
        r = union([big], [0.5])
        self.assertEqual(r.unified_type, LogicalType.FLOAT)
        self.assertTrue(r.precision.has_loss)
        self.assertEqual(r.precision.counts[("INT", "FLOAT")], 1)
        ev = r.precision.samples[0]
        self.assertEqual(ev.value, repr(big))
        self.assertEqual((ev.collection, ev.row), (0, 0))

    def test_small_int_to_float_lossless(self):
        r = union([1, 2], [0.5])
        self.assertFalse(r.precision.has_loss)

    def test_decimal_to_float_reported(self):
        r = union([Decimal("0.1")], [1.0])
        self.assertTrue(r.precision.has_loss)
        self.assertEqual(r.precision.counts[("DECIMAL", "FLOAT")], 1)

    def test_exact_decimal_to_float_lossless(self):
        r = union([Decimal("0.5")], [1.0])
        self.assertFalse(r.precision.has_loss)

    def test_int_to_decimal_lossless(self):
        r = union([2 ** 100], [Decimal("1.5")])
        self.assertEqual(r.unified_type, LogicalType.DECIMAL)
        self.assertFalse(r.precision.has_loss)
        self.assertIn(Decimal(2 ** 100), r.values)


if __name__ == "__main__":
    unittest.main()
