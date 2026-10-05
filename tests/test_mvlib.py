"""核心规则单元自测：python3 -m unittest discover -s tests -v"""
import unittest

from mvlib import Table, MaterializedView


def make(n=10):
    t = Table("orders")
    t.bulk_load([{"id": i, "region": "r%d" % (i % 2), "amount": i}
                 for i in range(1, n + 1)])
    mv = MaterializedView("mv", t, "region", "amount")
    mv.refresh()
    return t, mv


class TestIncrementalRules(unittest.TestCase):
    def test_insert(self):
        t, mv = make()
        t.insert({"id": 99, "region": "r0", "amount": 100})
        rep = mv.refresh()
        self.assertEqual(rep.mode, "incremental")
        self.assertEqual(mv.rows(), mv.rebuild() and mv.rows())

    def test_update_moves_group(self):
        t, mv = make()
        t.update(2, {"region": "r1", "amount": 50})
        mv.refresh()
        rows = dict((r[0], r) for r in mv.rows())
        self.assertEqual(rows["r1"][2], sum(r[2] for r in mv.rows()
                                            if r[0] == "r1"))

    def test_delete_empties_group(self):
        t, mv = make(2)  # r0:{1}, r1:{2}
        t.delete(1)
        mv.refresh()
        self.assertEqual([r[0] for r in mv.rows()], ["r0"])

    def test_min_max_after_deleting_extremes(self):
        t, mv = make()
        t.delete(10)  # r0 的最大值
        t.delete(9)   # r1 的最大值
        t.delete(1)   # r0 的最小值
        mv.refresh()
        mv2 = MaterializedView("v2", t, "region", "amount")
        mv2.rebuild()
        self.assertEqual(mv.rows(), mv2.rows())

    def test_update_equals_delete_plus_insert(self):
        t, mv = make()
        t.update(5, {"amount": 500})
        mv.refresh()
        rows = dict((r[0], r) for r in mv.rows())
        self.assertEqual(rows["r1"][3], 1)    # min 不变
        self.assertEqual(rows["r1"][4], 500)  # max 变为新值


class TestFallbackRules(unittest.TestCase):
    def test_truncate_fallback(self):
        t, mv = make()
        t.truncate()
        rep = mv.refresh()
        self.assertEqual(rep.mode, "full")
        self.assertEqual(mv.rows(), [])

    def test_schema_fallback(self):
        t, mv = make()
        t.alter_schema()
        self.assertEqual(mv.refresh().mode, "full")

    def test_log_gap_fallback(self):
        t, mv = make()
        t.insert({"id": 11, "region": "r0", "amount": 1})
        t.debug_corrupt_log_gap()
        rep = mv.refresh()
        self.assertEqual(rep.mode, "full")
        self.assertIn("缺口", rep.reason)

    def test_cost_fallback(self):
        t, mv = make(10)
        for i in range(1, 10):  # 删 9 行，存活 1 行：9 > 1 × 0.6
            t.delete(i)
        rep = mv.refresh()
        self.assertEqual(rep.mode, "full")
        self.assertIn("成本", rep.reason)

    def test_definition_change_forces_full(self):
        t, mv = make()
        mv.redefine(group_col="amount")
        self.assertEqual(mv.refresh().mode, "full")

    def test_fallback_result_still_correct(self):
        t, mv = make()
        t.truncate()
        t.insert({"id": 1, "region": "x", "amount": 7})
        mv.refresh()
        self.assertEqual(mv.rows(), [("x", 1, 7, 7, 7)])


class TestSnapshot(unittest.TestCase):
    def test_snapshot_is_repeatable(self):
        t, mv = make()
        snap = t.snapshot()
        before = list(t.iter_snapshot(snap))
        t.insert({"id": 11, "region": "r0", "amount": 1})
        t.delete(1)
        after = list(t.iter_snapshot(snap))
        self.assertEqual(before, after)
        self.assertEqual(len(before), 10)


if __name__ == "__main__":
    unittest.main()
