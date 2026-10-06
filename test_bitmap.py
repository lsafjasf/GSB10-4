"""test_bitmap.py — 自测 + 对拍（仅标准库 unittest）。

覆盖：
1. 边界用例：全空 / 全满 / 单位置 / 极稀疏 / 极稠密 / 随机多档密度 / 交替 / 连续块
2. 对拍 A：同一数据分别强制 RAW 与 SPARSE 编码，解码结果必须完全一致
3. 对拍 B：交并差在 RAW/SPARSE 任意混用下，结果与参考实现（Python int 集合运算）一致
4. 编码选择：选择结果与分布挂钩（空/满->SPARSE，半满->RAW）
5. 序列化往返

运行：python3 test_bitmap.py   （生成 difftest_results.csv）
"""

import csv
import hashlib
import random
import unittest

from bitmap_codec import (
    Bitmap, ENC_RAW, ENC_SPARSE, choose_encoding,
)

N = 100_000  # 对拍用位图长度
rng = random.Random(20261006)


def make_cases(n=N):
    cases = {}
    cases["empty"] = []
    cases["full"] = list(range(n))
    cases["single_first"] = [0]
    cases["single_last"] = [n - 1]
    cases["extreme_sparse"] = sorted(rng.sample(range(n), 10))
    cases["extreme_dense"] = sorted(set(range(n)) - set(rng.sample(range(n), 10)))
    for d in (0.001, 0.01, 0.1, 0.5, 0.9, 0.99):
        cases[f"random_{d}"] = sorted(rng.sample(range(n), int(n * d)))
    cases["alternating"] = list(range(0, n, 2))
    cases["block_head"] = list(range(n // 10))
    cases["block_tail"] = list(range(n - n // 10, n))
    return cases


CASES = make_cases()
DIFF_ROWS = []  # 对拍数据行，最终写 CSV


def digest(value):
    return hashlib.sha1(value.to_bytes(
        (value.bit_length() + 7) // 8 or 1, "little")).hexdigest()[:12]


def ref_int(positions):
    v = 0
    for p in positions:
        v |= 1 << p
    return v


REFS = {name: ref_int(pos) for name, pos in CASES.items()}


class TestRoundtrip(unittest.TestCase):
    def test_roundtrip_all_cases(self):
        for name, pos in CASES.items():
            with self.subTest(case=name):
                bm = Bitmap.from_positions(N, pos)
                self.assertEqual(bm.to_int(), REFS[name])
                self.assertEqual(bm.to_positions(), pos)
                self.assertEqual(bm.popcount(), len(pos))

    def test_serialize_roundtrip(self):
        for name, pos in CASES.items():
            with self.subTest(case=name):
                bm = Bitmap.from_positions(N, pos)
                bm2 = Bitmap.deserialize(bm.serialize())
                self.assertEqual(bm2.encoding, bm.encoding)
                self.assertEqual(bm2.negated, bm.negated)
                self.assertEqual(bm2, bm)

    def test_nbits_mismatch_raises(self):
        a = Bitmap.from_positions(100, [1])
        b = Bitmap.from_positions(200, [1])
        for op in (Bitmap.__and__, Bitmap.__or__, Bitmap.__xor__, Bitmap.__sub__):
            with self.assertRaises(ValueError):
                op(a, b)

    def test_out_of_range_position(self):
        with self.assertRaises(ValueError):
            Bitmap.from_positions(10, [10])
        with self.assertRaises(ValueError):
            Bitmap.from_positions(10, [-1])

    def test_choose_encoding_by_density(self):
        self.assertEqual(choose_encoding(N, 0)[0], ENC_SPARSE)      # 全空
        self.assertEqual(choose_encoding(N, N)[0], ENC_SPARSE)      # 全满
        self.assertEqual(choose_encoding(N, 5)[0], ENC_SPARSE)      # 极稀疏
        self.assertEqual(choose_encoding(N, N - 5)[0], ENC_SPARSE)  # 极稠密
        self.assertEqual(choose_encoding(N, N // 2)[0], ENC_RAW)    # 半满
        self.assertTrue(choose_encoding(N, N - 5)[1])               # 稠密取反


class TestCrossEncodingDecode(unittest.TestCase):
    """对拍 A：RAW 与 SPARSE 解码必须完全一致。"""

    def test_raw_vs_sparse_decode_equal(self):
        for name, pos in CASES.items():
            with self.subTest(case=name):
                raw = Bitmap.from_positions(N, pos, encoding=ENC_RAW)
                sparse = Bitmap.from_positions(N, pos, encoding=ENC_SPARSE)
                ok = raw.to_int() == sparse.to_int() == REFS[name]
                DIFF_ROWS.append([
                    "decode", name, N, len(pos), "RAW", "SPARSE",
                    len(pos), digest(REFS[name]), "PASS" if ok else "FAIL"])
                self.assertTrue(ok)


class TestMixedOps(unittest.TestCase):
    """对拍 B：交并差在两种编码任意混用下与参考结果一致。"""

    OPS = {
        "AND": (lambda a, b: a & b, lambda a, b: a & b),
        "OR": (lambda a, b: a | b, lambda a, b: a | b),
        "XOR": (lambda a, b: a ^ b, lambda a, b: a ^ b),
        "DIFF": (lambda a, b: a - b, lambda a, b: a & ~b),
    }

    def test_mixed_ops(self):
        pairs = [
            ("random_0.001", "random_0.5"),
            ("extreme_sparse", "extreme_dense"),
            ("empty", "full"),
            ("random_0.01", "random_0.99"),
            ("block_head", "block_tail"),
            ("alternating", "random_0.1"),
            ("single_first", "single_last"),
            ("extreme_sparse", "random_0.001"),
        ]
        encodings = [ENC_RAW, ENC_SPARSE]
        mask = (1 << N) - 1
        for left_name, right_name in pairs:
            for op_name, (bitmap_op, int_op) in self.OPS.items():
                for enc_l in encodings:
                    for enc_r in encodings:
                        with self.subTest(pair=(left_name, right_name),
                                          op=op_name, enc=(enc_l, enc_r)):
                            lb = Bitmap.from_positions(N, CASES[left_name], encoding=enc_l)
                            rb = Bitmap.from_positions(N, CASES[right_name], encoding=enc_r)
                            got = bitmap_op(lb, rb).to_int()
                            want = int_op(REFS[left_name], REFS[right_name]) & mask
                            ok = got == want
                            DIFF_ROWS.append([
                                op_name, f"{left_name}&{right_name}", N,
                                bin(want).count("1"),
                                {0: "RAW", 1: "SPARSE"}[enc_l],
                                {0: "RAW", 1: "SPARSE"}[enc_r],
                                bin(want).count("1"), digest(want),
                                "PASS" if ok else "FAIL"])
                            self.assertEqual(got, want)


def write_csv(path="difftest_results.csv"):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "case", "nbits", "popcount_or_result",
                    "enc_left", "enc_right", "result_popcount",
                    "result_sha1_12", "verdict"])
        w.writerows(DIFF_ROWS)
    return path


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestRoundtrip))
    suite.addTests(loader.loadTestsFromTestCase(TestCrossEncodingDecode))
    suite.addTests(loader.loadTestsFromTestCase(TestMixedOps))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    path = write_csv()
    total = len(DIFF_ROWS)
    passed = sum(1 for r in DIFF_ROWS if r[-1] == "PASS")
    print(f"\n对拍数据已写入 {path}: {passed}/{total} 条 PASS")
    raise SystemExit(0 if result.wasSuccessful() and passed == total else 1)
