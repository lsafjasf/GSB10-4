"""对拍测试：加载 data/differential_cases.json，以打乱顺序重算并与金标准比对。"""
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setops import Dataset, IncomparableTypesError, Type
from setops.encoding import decode_row, encode_row

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "differential_cases.json")


def _load():
    with open(DATA, encoding="utf-8") as f:
        return json.load(f)


def _run_case(case, shuffle_seed=None, swap=False):
    lt = [Type(t) for t in case["left_types"]]
    rt = [Type(t) for t in case["right_types"]]
    left = [decode_row(r) for r in case["left_rows"]]
    right = [decode_row(r) for r in case["right_rows"]]
    if shuffle_seed is not None:
        rng = random.Random(shuffle_seed)
        rng.shuffle(left)
        rng.shuffle(right)
    if swap:
        lt, rt = rt, lt
        left, right = right, left
    a = Dataset(lt, left, name="left")
    b = Dataset(rt, right, name="right")
    return getattr(a, case["op"])(b)


class TestDifferential(unittest.TestCase):
    def test_all_cases_against_golden(self):
        payload = _load()
        cases = payload["cases"]
        self.assertEqual(len(cases), payload["case_count"])
        n_ok = n_err = 0
        for case in cases:
            expected = case["expected"]
            with self.subTest(case=case["name"]):
                if expected["kind"] == "error":
                    n_err += 1
                    for seed in (None, 1, 2, 3):
                        with self.assertRaises(IncomparableTypesError) as ctx:
                            _run_case(case, shuffle_seed=seed)
                        self.assertEqual(ctx.exception.column, expected["column"])
                    continue
                n_ok += 1
                # 交换操作数只对可交换运算合法
                variants = [(None, False), (11, False), (22, False), (33, False)]
                if case["op"] in ("union", "intersection"):
                    variants.append((44, True))
                golden_keys = None
                for seed, swap in variants:
                    result = _run_case(case, shuffle_seed=seed, swap=swap)
                    self.assertEqual([t.value for t in result.types],
                                     expected["types"])
                    got = sorted(
                        (encode_row(r) for r in result.rows),
                        key=lambda r: json.dumps(r, sort_keys=True,
                                                 ensure_ascii=False),
                    )
                    self.assertEqual(got, expected["rows"],
                                     f"顺序变体 seed={seed} swap={swap} 结果不同")
                    self.assertEqual(result.precision.total_loss_count,
                                     expected["precision_loss"])
                    if golden_keys is None:
                        golden_keys = result.key_set()
                    else:
                        self.assertEqual(golden_keys, result.key_set())
        print(f"\n对拍通过: {n_ok} 个成功类用例, {n_err} 个拒绝类用例, "
              f"每个成功用例 x{4} 种顺序变体")


if __name__ == "__main__":
    unittest.main()
