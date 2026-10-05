"""stable_sort 自测 + 对拍（仅标准库 unittest）。

参考实现 reference_sort 与被测实现算法独立：
- 被测实现：一次性把 10 级键 + 原始下标组成元组排序；
- 参考实现：从最低优先级键开始，逐级做稳定排序（稳定性传播）。
两者结果必须完全一致。
"""
import json
import os
import random
import unittest

from stable_sort import stable_sort

HERE = os.path.dirname(os.path.abspath(__file__))


def ref_rank(v):
    if isinstance(v, bool):
        return (1, v)
    if isinstance(v, (int, float)):
        return (0, float(v))
    if isinstance(v, str):
        return (2, v)
    return (3, repr(v))


def ref_missing(v):
    return v is None or (isinstance(v, float) and v != v)


class Rev:
    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    def __lt__(self, other):
        return other.v < self.v

    def __eq__(self, other):
        return self.v == other.v


def reference_sort(rows, specs):
    parsed = []
    for spec in specs:
        parsed.append((spec, False) if isinstance(spec, str) else (spec[0], bool(spec[1])))

    result = list(rows)
    # 多趟稳定排序：最低优先级先排，最高优先级最后排
    for name, descending in reversed(parsed):
        def one_key(row, name=name, descending=descending):
            v = row.get(name)
            if ref_missing(v):
                return (1,)  # 缺失恒最后，不随 reverse 翻转
            return (0, Rev(ref_rank(v)) if descending else ref_rank(v))

        result.sort(key=one_key)
    return result


def gen_dataset(n, seed):
    rng = random.Random(seed)
    pool = [1, 2, 2, 3, "a", "b", None, True, False, [1, 2], {"x": 1}]
    rows = []
    for _ in range(n):
        rows.append({
            "k1": rng.choice(pool),
            "k2": rng.choice(pool),
            "k3": rng.choice(pool),
            "k4": rng.choice(pool),
        })
    return rows


class TestStableSort(unittest.TestCase):
    def test_all_ties_keep_original_order(self):
        # 全部平局：所有键等价，必须严格保持原始顺序
        rows = [{"k": 1, "id": i} for i in range(1000)]
        out = stable_sort(rows, ["k"])
        self.assertEqual([r["id"] for r in out], list(range(1000)))
        # 重复排序完全一致
        self.assertEqual(stable_sort(out, ["k"]), out)

    def test_no_ties_at_all(self):
        # 完全没有平局：主键全唯一，结果即按键升序
        rows = [{"k": 999 - i, "id": i} for i in range(1000)]
        out = stable_sort(rows, ["k"])
        self.assertEqual([r["k"] for r in out], list(range(1000)))

    def test_missing_keys_always_last_and_deterministic(self):
        # 部分键为空（None / 不存在 / NaN）：恒排最后，组内保持原序
        rows = [
            {"k": 2, "id": 0},
            {"k": None, "id": 1},
            {"k": 1, "id": 2},
            {"id": 3},                      # 键缺失
            {"k": float("nan"), "id": 4},   # NaN
            {"k": 2, "id": 5},
        ]
        out = stable_sort(rows, ["k"])
        self.assertEqual([r["id"] for r in out], [2, 0, 5, 1, 3, 4])
        # 降序时缺失同样在最后
        out_desc = stable_sort(rows, [("k", True)])
        self.assertEqual([r["id"] for r in out_desc], [0, 5, 2, 1, 3, 4])
        # 重复排序一致
        self.assertEqual(stable_sort(out_desc, [("k", True)]), out_desc)

    def test_incomparable_keys_fallback(self):
        # int / str / dict / bool 不可比：按类型秩回退，结果确定
        rows = [
            {"k": "z", "id": 0},
            {"k": 9, "id": 1},
            {"k": {"a": 1}, "id": 2},
            {"k": True, "id": 3},
            {"k": 1, "id": 4},
            {"k": "a", "id": 5},
        ]
        out = stable_sort(rows, ["k"])
        # 秩: 数字(1,9) -> bool(True=1) -> str(a,z) -> dict(repr)
        self.assertEqual([r["id"] for r in out], [4, 1, 3, 5, 0, 2])
        # 多次结果完全一致（不抛 TypeError）
        self.assertEqual(stable_sort(rows, ["k"]), out)

    def test_multi_level_and_directions(self):
        rows = [
            {"a": 1, "b": 2, "id": 0},
            {"a": 1, "b": 1, "id": 1},
            {"a": 1, "b": 2, "id": 2},
            {"a": 0, "b": 9, "id": 3},
        ]
        out = stable_sort(rows, ["a", ("b", True)])
        self.assertEqual([r["id"] for r in out], [3, 0, 2, 1])

    def test_cross_check_with_reference(self):
        # 对拍：两种独立实现必须逐元素一致（含 10 级混合升降序）
        specs = ["k1", ("k2", True), "k3", ("k4", True)]
        for seed in range(5):
            rows = gen_dataset(5000, seed)
            self.assertEqual(stable_sort(rows, specs),
                             reference_sort(rows, specs),
                             msg=f"seed={seed}")

    def test_repeated_sort_identical(self):
        rows = gen_dataset(10000, 42)
        specs = ["k1", ("k2", True), "k3", "k4"]
        first = stable_sort(rows, specs)
        for _ in range(3):
            self.assertEqual(stable_sort(rows, specs), first)
        # 对已排好序的数据再排，顺序不变（幂等）
        self.assertEqual(stable_sort(first, specs), first)

    def test_frozen_pair_data(self):
        # 对拍数据文件：冻结的小样本输入与期望输出
        with open(os.path.join(HERE, "testdata", "pair_input.json")) as f:
            rows = json.load(f)
        with open(os.path.join(HERE, "testdata", "pair_expected.json")) as f:
            expected = json.load(f)
        specs = ["k1", ("k2", True), "k3"]
        got = stable_sort(rows, specs)
        self.assertEqual(got, expected)
        self.assertEqual(reference_sort(rows, specs), expected)

    def test_primary_key_resort_explained(self):
        # 差异说明的可执行版本：
        # 1) 多级排序结果再“只按主键稳定重排”，结果不变（多级结果本身就是
        #    主键有序、主键相等块内按次级键有序的）；
        # 2) 直接对原始数据只按主键排，与多级排序仅在“主键相等块”内部不同，
        #    两者主键序列完全一致、且行集合相同。
        rows = gen_dataset(3000, 7)
        multi = stable_sort(rows, ["k1", ("k2", True), "k3"])
        re_by_primary = stable_sort(multi, ["k1"])
        self.assertEqual(re_by_primary, multi)  # 主键重排是恒等操作

        only_primary = stable_sort(rows, ["k1"])
        self.assertEqual([r["k1"] for r in only_primary],
                         [r["k1"] for r in multi])       # 主键序列一致
        self.assertEqual(sorted(id(r) for r in only_primary),
                         sorted(id(r) for r in multi))    # 行集合一致
        self.assertNotEqual(only_primary, multi)          # 块内顺序不同


if __name__ == "__main__":
    unittest.main(verbosity=2)
