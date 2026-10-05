"""stable_sort_pkg 自测（python3 -m unittest -v）。

覆盖情形：
  1. 全部平局：所有业务键相同 -> 严格保持原始顺序
  2. 完全没有平局：主键唯一 -> 与单键排序一致
  3. 部分键为空：字段缺失 / None / 取值函数抛异常，NULLS FIRST/LAST
  4. 键不可比：int 与 str 混排、NaN —— 确定性回退
  5. 稳定性：同一批数据重复排序逐位一致（含打乱输入后等值语义）
  6. 对拍：一次成型 stable_sort == 逐级稳定 multipass_sort（随机数据）
  7. 与"只按主键重排"的差异解释：等值组内重排、组间不动、区间连续
"""

import math
import random
import unittest

from stable_sort_pkg import SortKey, MISSING, stable_sort, multipass_sort, explain_vs_primary


def ids(records):
    return [r["id"] for r in records]


class TestAllTies(unittest.TestCase):
    def test_all_ties_preserves_input_order(self):
        rows = [{"id": i, "a": 1, "b": "x"} for i in range(20)]
        keys = [SortKey("a"), SortKey("b")]
        self.assertEqual(ids(stable_sort(rows, keys)), list(range(20)))

    def test_all_ties_repeated_runs_identical(self):
        rows = [{"id": i, "a": 1} for i in range(50)]
        first = stable_sort(rows, [SortKey("a"), SortKey("a")])
        for _ in range(5):
            self.assertEqual(first, stable_sort(rows, [SortKey("a"), SortKey("a")]))

    def test_all_ties_desc_still_stable(self):
        rows = [{"id": i, "a": 7} for i in range(10)]
        self.assertEqual(ids(stable_sort(rows, [SortKey("a", descending=True)])),
                         list(range(10)))


class TestNoTies(unittest.TestCase):
    def test_unique_primary_matches_single_key(self):
        rows = [{"id": i, "a": 100 - i, "b": i % 3} for i in range(30)]
        keys = [SortKey("a"), SortKey("b", descending=True)]
        result = stable_sort(rows, keys)
        only_primary = stable_sort(rows, keys[:1])
        self.assertEqual(result, only_primary)
        self.assertEqual(ids(result), list(reversed(range(30))))


class TestMissingKeys(unittest.TestCase):
    def setUp(self):
        # 4 条：正常值 2 条、字段缺失 1 条、显式 None 1 条
        self.rows = [
            {"id": 0, "a": 5},
            {"id": 1},
            {"id": 2, "a": None},
            {"id": 3, "a": 2},
        ]

    def test_nulls_last_default(self):
        result = stable_sort(self.rows, [SortKey("a")])
        self.assertEqual(ids(result), [3, 0, 1, 2])

    def test_nulls_first(self):
        result = stable_sort(self.rows, [SortKey("a", nulls="first")])
        # 两个空值平局 -> 原始下标兜底：id 1 在 id 2 前
        self.assertEqual(ids(result), [1, 2, 3, 0])

    def test_nulls_ignore_descending(self):
        result = stable_sort(self.rows, [SortKey("a", descending=True)])
        # 降序只反转正常值；空值仍在最后且保原始序
        self.assertEqual(ids(result), [0, 3, 1, 2])

    def test_extractor_exception_is_missing(self):
        rows = [{"id": 0, "a": 1}, {"id": 1, "a": 2}]

        def boom(row):
            if row["id"] == 1:
                raise RuntimeError("bad key")
            return row["a"]

        result = stable_sort(rows, [SortKey(boom)])
        self.assertEqual(ids(result), [0, 1])

    def test_secondary_key_missing_breaks_ties_deterministically(self):
        rows = [
            {"id": 0, "a": 1, "b": 9},
            {"id": 1, "a": 1},          # b 缺失
            {"id": 2, "a": 1, "b": 3},
        ]
        result = stable_sort(rows, [SortKey("a"), SortKey("b")])
        # 主键全平；次级 b：3, 9 在前，缺失在最后；缺失的只有一条
        self.assertEqual(ids(result), [2, 0, 1])
        # 再来一次逐位一致
        self.assertEqual(ids(stable_sort(rows, [SortKey("a"), SortKey("b")])),
                         [2, 0, 1])


class TestIncomparable(unittest.TestCase):
    def test_int_and_str_deterministic_fallback(self):
        rows = [
            {"id": 0, "a": 10},
            {"id": 1, "a": "b"},
            {"id": 2, "a": 2},
            {"id": 3, "a": "a"},
        ]
        keys = [SortKey("a")]
        r1 = ids(stable_sort(rows, keys))
        r2 = ids(stable_sort(rows, keys))
        self.assertEqual(r1, r2, "不可比回退必须可复现")
        # 同类型按自然序：int 2 < 10；str 'a' < 'b'
        # 跨类型按 (module, qualname, repr)：builtins.int < builtins.str
        self.assertEqual(r1, [2, 0, 3, 1])

    def test_nan_goes_last_and_is_stable(self):
        rows = [
            {"id": 0, "a": 1.0},
            {"id": 1, "a": float("nan")},
            {"id": 2, "a": -3.0},
            {"id": 3, "a": float("nan")},
        ]
        result = stable_sort(rows, [SortKey("a")])
        # 正常值 -3, 1；两个 NaN 不可比，互相平局，按原始序 1 在 3 前
        self.assertEqual(ids(result), [2, 0, 1, 3])

    def test_nan_before_missing(self):
        rows = [
            {"id": 0, "a": float("nan")},
            {"id": 1},
            {"id": 2, "a": 1.0},
        ]
        # 桶序：present < missing < incomparable
        self.assertEqual(ids(stable_sort(rows, [SortKey("a")])), [2, 1, 0])

    def test_incomparable_reversed_by_descending(self):
        rows = [{"id": 0, "a": 10}, {"id": 1, "a": "b"},
                {"id": 2, "a": 2}, {"id": 3, "a": "a"}]
        result = stable_sort(rows, [SortKey("a", descending=True)])
        # descending 反转该级完整全序（含跨类型回退序）：
        # 升序为 2,10,'a','b'，降序即 'b','a',10,2
        self.assertEqual(ids(result), [1, 3, 0, 2])


class TestDeterminism(unittest.TestCase):
    def test_same_batch_repeated_sort_identical(self):
        rng = random.Random(20261005)
        rows = [{"id": i,
                 "a": rng.randrange(0, 5),
                 "b": rng.choice(["x", "y", "z", None]),
                 "c": rng.randrange(0, 3)}
                for i in range(500)]
        keys = [SortKey("a"), SortKey("b"), SortKey("c", descending=True)]
        first = stable_sort(rows, keys)
        for _ in range(10):
            again = stable_sort(rows, keys)
            self.assertEqual([id(x) for x in first], [id(x) for x in again])

    def test_shuffled_input_equal_elements_keep_their_relative_semantics(self):
        # 输入整体打乱：等价元素的"相对次序"按它们在当次输入中的位置定义。
        base = [{"id": i, "a": 1} for i in range(15)]
        shuffled = base[:]
        random.Random(7).shuffle(shuffled)
        result = stable_sort(shuffled, [SortKey("a")])
        self.assertEqual(result, shuffled)  # 全平局：输出就是输入
        # 输出与输入是同一批对象，无丢失/重复
        self.assertEqual(sorted(ids(result)), list(range(15)))


class TestCrossCheck(unittest.TestCase):
    def _random_rows(self, seed, n=800):
        rng = random.Random(seed)
        pools = {
            "k1": [0, 1, 2, None],
            "k2": ["a", "b", "c", None],
            "k3": [10, 20, 30, 40],
            "k4": [True, False],
            "k5": ["p", 5, "q", 9],  # 故意混入不可比类型
        }
        rows = []
        for i in range(n):
            row = {"id": i}
            for name, pool in pools.items():
                value = rng.choice(pool)
                if value is not None:
                    row[name] = value
            if rng.random() < 0.03:
                row["k5"] = float("nan")
            rows.append(row)
        return rows

    def test_two_implementations_agree_many_seeds(self):
        specs = [
            [SortKey("k1"), SortKey("k2"), SortKey("k3", descending=True),
             SortKey("k4"), SortKey("k5")],
            [SortKey("k1", nulls="first"), SortKey("k5"),
             SortKey("k2", descending=True, nulls="first")],
            [SortKey("k3"), SortKey("k3"), SortKey("k3")],  # 重复键
        ]
        for seed in range(20):
            rows = self._random_rows(seed)
            for spec in specs:
                a = [id(r) for r in stable_sort(rows, spec)]
                b = [id(r) for r in multipass_sort(rows, spec)]
                self.assertEqual(a, b, f"seed={seed} 两实现不一致")

    def test_permutation_no_loss_no_duplicate(self):
        rows = self._random_rows(99, n=1000)
        result = stable_sort(rows, [SortKey("k1"), SortKey("k2"), SortKey("k5")])
        self.assertEqual(sorted(ids(result)), list(range(1000)))


class TestExplainVsPrimary(unittest.TestCase):
    def test_movement_only_inside_tie_groups(self):
        rows = [
            {"id": 0, "a": 1, "b": 30},
            {"id": 1, "a": 1, "b": 10},
            {"id": 2, "a": 1, "b": 20},
            {"id": 3, "a": 2, "b": 1},
            {"id": 4, "a": 0, "b": 9},
        ]
        report = explain_vs_primary(rows, [SortKey("a"), SortKey("b")])
        self.assertEqual(report["total"], 5)
        self.assertEqual(report["tie_group_count"], 1)
        self.assertEqual(report["tie_group_size_distribution"], {3: 1})
        self.assertEqual(report["moved_within_primary_tie_groups"], 3)
        for ex in report["examples"]:
            self.assertTrue(ex["contiguous_in_multi"])
        # 组内按 b 重排：id 1(b=10),2(b=20),0(b=30)；组间主键 0,1,2 不动
        self.assertEqual(
            ids(stable_sort(rows, [SortKey("a"), SortKey("b")])),
            [4, 1, 2, 0, 3],
        )

    def test_no_ties_no_difference(self):
        rows = [{"id": i, "a": i, "b": -i} for i in range(10)]
        report = explain_vs_primary(rows, [SortKey("a"), SortKey("b")])
        self.assertEqual(report["tie_group_count"], 0)
        self.assertEqual(report["moved_within_primary_tie_groups"], 0)


class TestMisc(unittest.TestCase):
    def test_function_field(self):
        rows = [{"id": 0, "a": -5}, {"id": 1, "a": 2}]
        result = stable_sort(rows, [SortKey(lambda r: abs(r["a"]))])
        self.assertEqual(ids(result), [1, 0])

    def test_invalid_args(self):
        with self.assertRaises(ValueError):
            stable_sort([], [])
        with self.assertRaises(ValueError):
            SortKey("a", nulls="bad")

    def test_missing_sentinel_singleton_and_repr(self):
        self.assertIs(MISSING, type(MISSING)())
        self.assertEqual(repr(MISSING), "<MISSING>")

    def test_naive_sorted_python3_crashes_but_we_do_not(self):
        # 对照：原生 sorted 对混合类型直接抛 TypeError，本库必须正常给出确定结果
        rows = [{"id": 0, "a": 1}, {"id": 1, "a": "x"}]
        with self.assertRaises(TypeError):
            sorted(rows, key=lambda r: r["a"])
        self.assertEqual(ids(stable_sort(rows, [SortKey("a")])), [0, 1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
