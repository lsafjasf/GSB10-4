"""边界用例（unittest）：

E01 NULL = NULL 在内连接中不匹配
E02 左外连接：左行空外键 -> 右侧整行补 NULL
E03 右外连接：右行 NULL 键 -> 左侧整行补 NULL
E04 全外连接：两表各自的空外键行都补 NULL，且互不匹配
E05 半连接：空外键左行一律排除
E06 反半连接：空外键左行保留(NOT EXISTS 语义)
E07 SQL NOT IN 陷阱对照：右表键列含 NULL 时 NOT IN 语义为空集，
    NOT EXISTS / anti_join 不受影响
E08 重复键多对多：两边各 N 行同键 -> N*N 行
E09 空表参与各关联不报错、宽度推断为 0
E10 稳定输出：同数据重复执行结果完全一致；NULL 规范排在末尾
E11 key 既支持列下标也支持 callable
E12 左右表同一值列表含 NULL/非空混合，命中与未命中交错
E13 独有键：仅左表或仅右表存在的键
E14 右表全 NULL：内连接空，左外全补空，右外左侧补空
"""
import os
import sqlite3
import unittest

import null_join as nj

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(HERE, "reports")


class EdgeCases(unittest.TestCase):
    def test_e01_null_neq_null_inner(self):
        self.assertEqual(nj.inner_join([(None,)], [(None,)], 0, 0), [])
        self.assertEqual(nj.inner_join([(None,)], [(None, 1)], 0, 0), [])

    def test_e02_left_join_null_fk(self):
        out = nj.left_join([(None, "l1"), (1, "l2")], [(1, "r1")], 0, 0)
        self.assertEqual(nj.sort_rows(out),
                         [(1, "l2", 1, "r1"), (None, "l1", None, None)])

    def test_e03_right_join_null_key(self):
        out = nj.right_join([(1, "l1")], [(None, "r1"), (1, "r2")], 0, 0)
        self.assertEqual(nj.sort_rows(out),
                         [(1, "l1", 1, "r2"), (None, None, None, "r1")])

    def test_e04_full_join_nulls_both_sides(self):
        out = nj.full_join([(None, "l1"), (2, "l2")],
                           [(None, "r1"), (2, "r2")], 0, 0)
        self.assertEqual(nj.sort_rows(out), [
            (2, "l2", 2, "r2"),
            (None, "l1", None, None),
            (None, None, None, "r1"),
        ])

    def test_e05_semi_excludes_null(self):
        out = nj.semi_join([(None, "l1"), (1, "l2")], [(None, "rx"), (1, "r1")], 0, 0)
        self.assertEqual(out, [(1, "l2")])  # 即使右表也有 NULL，也不匹配

    def test_e06_anti_keeps_null(self):
        out = nj.anti_join([(None, "l1"), (1, "l2"), (9, "l3")],
                           [(1, "r1")], 0, 0)
        self.assertEqual(nj.sort_rows(out), [(9, "l3"), (None, "l1")])

    def test_e07_notin_vs_notexists_trap(self):
        # 右表键列混入一个 NULL。
        left = [(1, "a"), (2, "b"), (3, "c")]
        right = [(1, "x"), (None, "z")]
        con = sqlite3.connect(":memory:")
        cur = con.cursor()
        cur.execute("CREATE TABLE L(k INT, v TEXT)")
        cur.execute("CREATE TABLE R(k INT, v TEXT)")
        cur.executemany("INSERT INTO L VALUES(?,?)", left)
        cur.executemany("INSERT INTO R VALUES(?,?)", right)
        not_in = cur.execute(
            "SELECT k, v FROM L WHERE k NOT IN (SELECT k FROM R)").fetchall()
        not_exists = cur.execute(
            "SELECT k, v FROM L WHERE NOT EXISTS "
            "(SELECT 1 FROM R WHERE L.k = R.k)").fetchall()
        con.close()
        # SQL 三值逻辑陷阱：NOT IN 子查询含 NULL -> 全部 UNKNOWN -> 空集
        self.assertEqual(not_in, [])
        # NOT EXISTS 语义：未匹配即保留
        self.assertEqual(not_exists, [(2, "b"), (3, "c")])
        # 本库 anti_join 采用 NOT EXISTS 语义
        self.assertEqual(nj.sort_rows(nj.anti_join(left, right, 0, 0)),
                         nj.sort_rows(not_exists))

    def test_e08_many_to_many(self):
        l = [(7, f"l{i}") for i in range(3)]
        r = [(7, f"r{i}") for i in range(4)]
        self.assertEqual(len(nj.inner_join(l, r, 0, 0)), 12)
        self.assertEqual(len(nj.semi_join(l, r, 0, 0)), 3)  # 半连接不展开

    def test_e09_empty_tables(self):
        self.assertEqual(nj.inner_join([], [(1, "r")], 0, 0), [])
        self.assertEqual(nj.left_join([], [(1, "r")], 0, 0), [])
        # 左表为空时左外为空；右外结果全部补 NULL
        self.assertEqual(nj.right_join([], [(1, "r")], 0, 0, lwidth=2),
                         [(None, None, 1, "r")])
        self.assertEqual(nj.full_join([], [], 0, 0, lwidth=2, rwidth=2), [])
        self.assertEqual(nj.semi_join([], [(1, "r")], 0, 0), [])
        self.assertEqual(nj.anti_join([], [(1, "r")], 0, 0), [])
        # 无显式宽度且表为空：推断为 0，不抛异常
        self.assertEqual(nj.right_join([], [(1, "r")], 0, 0),
                         [(1, "r")])

    def test_e10_stable_output(self):
        l = [(None, "a"), (1, "b"), (None, "c"), (2, "d")]
        r = [(1, "x"), (None, "y"), (2, "z")]
        runs = [nj.sort_rows(nj.full_join(l, r, 0, 0)) for _ in range(5)]
        for other in runs[1:]:
            self.assertEqual(runs[0], other)
        # NULL 在规范排序中位于非 NULL 之后
        ordered = nj.sort_rows([(None,), (1,), (0,), (None,)])
        self.assertEqual(ordered, [(0,), (1,), (None,), (None,)])

    def test_e11_key_callable(self):
        l = [(1, "a", "L"), (None, "b", "L")]
        r = [(1, "c", "R")]
        out = nj.inner_join(l, r, lambda row: row[0], lambda row: row[0])
        self.assertEqual(out, [(1, "a", "L", 1, "c", "R")])

    def test_e12_mixed_interleave(self):
        l = [(None, "n1"), (5, "m1"), (None, "n2"), (5, "m2"), (6, "m3")]
        r = [(5, "R1"), (None, "Rn"), (7, "R2")]
        inner = nj.sort_rows(nj.inner_join(l, r, 0, 0))
        self.assertEqual(inner, [(5, "m1", 5, "R1"), (5, "m2", 5, "R1")])
        semi = nj.sort_rows(nj.semi_join(l, r, 0, 0))
        self.assertEqual(semi, [(5, "m1"), (5, "m2")])
        anti = nj.sort_rows(nj.anti_join(l, r, 0, 0))
        self.assertEqual(anti, [(6, "m3"), (None, "n1"), (None, "n2")])

    def test_e13_solo_keys(self):
        l = [(42, "only_left")]
        r = [(84, "only_right")]
        self.assertEqual(nj.inner_join(l, r, 0, 0), [])
        self.assertEqual(nj.left_join(l, r, 0, 0), [(42, "only_left", None, None)])
        self.assertEqual(nj.right_join(l, r, 0, 0), [(None, None, 84, "only_right")])
        self.assertEqual(nj.sort_rows(nj.full_join(l, r, 0, 0)),
                         [(42, "only_left", None, None),
                          (None, None, 84, "only_right")])

    def test_e14_right_all_null(self):
        l = [(1, "a"), (2, "b")]
        r = [(None, "x"), (None, "y")]
        self.assertEqual(nj.inner_join(l, r, 0, 0), [])
        self.assertEqual(nj.sort_rows(nj.left_join(l, r, 0, 0)),
                         [(1, "a", None, None), (2, "b", None, None)])
        self.assertEqual(nj.sort_rows(nj.right_join(l, r, 0, 0)),
                         [(None, None, None, "x"), (None, None, None, "y")])
        self.assertEqual(nj.semi_join(l, r, 0, 0), [])
        self.assertEqual(nj.sort_rows(nj.anti_join(l, r, 0, 0)),
                         [(1, "a"), (2, "b")])


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EdgeCases)
    result = runner.run(suite)
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, "edge_report.txt"), "w", encoding="utf-8") as f:
        f.write(f"边界用例: {result.testsRun} 个, 成功={result.wasSuccessful()}\n")
    raise SystemExit(0 if result.wasSuccessful() else 1)
