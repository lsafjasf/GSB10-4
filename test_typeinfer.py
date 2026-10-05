"""typeinfer 自测（标准库 unittest）。运行: python3 test_typeinfer.py -v"""

import csv
import os
import random
import tempfile
import unittest

from typeinfer import (
    detect_scalar, infer_column, infer_columns, infer_csv,
    parse_value, wilson_lower_bound,
)


def col(kind, n, seed=0):
    """生成指定类型的 n 个值。"""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        if kind == "int":
            out.append(str(rng.randint(-10**6, 10**6)))
        elif kind == "float":
            out.append(f"{rng.uniform(-1000, 1000):.6f}")
        elif kind == "bool":
            out.append(rng.choice(["true", "false", "yes", "no"]))
        elif kind == "date":
            out.append(f"2024-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}")
        elif kind == "string":
            out.append("name_" + str(rng.randint(0, 10**9)))
        elif kind == "empty":
            out.append(rng.choice(["", "null", "NA"]))
    return out


class TestScalarDetect(unittest.TestCase):
    def test_basic_types(self):
        self.assertEqual(detect_scalar("42"), "int")
        self.assertEqual(detect_scalar("-7"), "int")
        self.assertEqual(detect_scalar("3.14"), "float")
        self.assertEqual(detect_scalar("1e5"), "float")
        self.assertEqual(detect_scalar("true"), "bool")
        self.assertEqual(detect_scalar("No"), "bool")
        self.assertEqual(detect_scalar("2024-01-31"), "date")
        self.assertEqual(detect_scalar("hello world"), "string")

    def test_parse_roundtrip(self):
        self.assertEqual(parse_value("42", "int"), 42)
        self.assertAlmostEqual(parse_value("2.5", "float"), 2.5)
        self.assertIs(parse_value("yes", "bool"), True)
        self.assertIsNone(parse_value("", "int"))
        with self.assertRaises(ValueError):
            parse_value("abc", "int")


class TestPureColumns(unittest.TestCase):
    def test_pure_int(self):
        r = infer_column(col("int", 500), "i")
        self.assertEqual((r.status, r.type), ("ok", "int"))
        self.assertGreaterEqual(r.confidence, 0.95)
        self.assertGreaterEqual(r.holdout_pass_rate, 0.95)

    def test_pure_float(self):
        r = infer_column(col("float", 500), "f")
        self.assertEqual((r.status, r.type), ("ok", "float"))

    def test_pure_text(self):
        r = infer_column(col("string", 500), "s")
        self.assertEqual((r.status, r.type), ("ok", "string"))

    def test_pure_bool_and_date(self):
        self.assertEqual(infer_column(col("bool", 300), "b").type, "bool")
        self.assertEqual(infer_column(col("date", 300), "d").type, "date")

    def test_nulls_do_not_block_inference(self):
        vals = col("int", 200) + ["", "null", "NA"] * 20
        r = infer_column(vals, "n")
        self.assertEqual((r.status, r.type), ("ok", "int"))
        self.assertEqual(r.null_count, 60)


class TestEmptyColumn(unittest.TestCase):
    def test_all_empty(self):
        r = infer_column(["", "null", "NA", "n/a", "-"] * 10, "e")
        self.assertEqual((r.status, r.type), ("empty", "empty"))
        self.assertEqual(r.null_count, 50)

    def test_zero_rows(self):
        r = infer_column([], "z")
        self.assertEqual(r.status, "empty")


class TestInsufficientSample(unittest.TestCase):
    def test_too_few_samples_refuses_conclusion(self):
        r = infer_column(col("int", 5), "tiny", min_samples=30)
        self.assertEqual(r.status, "insufficient_sample")
        self.assertIsNone(r.type)                 # 不得硬给类型
        self.assertEqual(r.sample_size, 5)        # 必须报出样本量
        self.assertEqual(r.required_samples, 30)

    def test_single_type_but_weak_confidence_is_insufficient_not_mixed(self):
        # 30 个非空 -> 训练 22 条，不足 min_samples=30
        r = infer_column(col("int", 30), "edge", min_samples=30)
        self.assertEqual(r.status, "insufficient_sample")
        # 训练 60 条全为 int：达到 min_samples，但 Wilson 下界 ~0.94 < 0.95，
        # 属于"样本不足以支撑结论"，而不是混合类型。
        r = infer_column(col("int", 80), "edge2", min_samples=30)
        self.assertEqual(r.status, "insufficient_sample")
        self.assertIsNone(r.type)
        self.assertEqual(r.type_counts, {"int": 60})
        # 样本足够（训练 ~150 条）后同一分布即可下结论
        r = infer_column(col("int", 200), "edge3", min_samples=30)
        self.assertEqual((r.status, r.type), ("ok", "int"))


class TestMixedColumns(unittest.TestCase):
    def setUp(self):
        self.mixed_num = col("int", 300, 1) + col("float", 120, 2)
        self.mixed_text = col("int", 300, 3) + col("string", 120, 4)

    def test_reject_is_default(self):
        r = infer_column(self.mixed_text, "m")
        self.assertEqual(r.status, "mixed")
        self.assertIsNone(r.type)
        self.assertEqual(r.decision["action"], "reject")
        self.assertIn("cost", r.decision)

    def test_promote_int_float_to_float(self):
        r = infer_column(self.mixed_num, "m", mixed_policy="promote")
        self.assertEqual((r.status, r.type), ("ok", "float"))
        self.assertEqual(r.decision["action"], "promote")
        self.assertIn("精度", r.decision["cost"])

    def test_promote_with_text_goes_string(self):
        r = infer_column(self.mixed_text, "m", mixed_policy="promote")
        self.assertEqual((r.status, r.type), ("ok", "string"))

    def test_split_reports_minority_cost(self):
        # 少数派占 ~28%，留出集必然验证不过 0.95；放宽验证阈值专注考察 split 决策本身
        r = infer_column(self.mixed_text, "m", mixed_policy="split",
                         validation_threshold=0.5)
        self.assertEqual((r.status, r.type), ("ok", "int"))
        self.assertEqual(r.decision["action"], "split")
        self.assertIn("string", r.decision["detail"])

    def test_tiny_minority_still_reports_confidence(self):
        # 1% 少数派：纯度置信度仍 >= 0.95，判为主类型，但置信度 < 1 如实反映风险
        vals = col("int", 2000, 5) + col("string", 20, 6)
        r = infer_column(vals, "r")
        self.assertEqual(r.type, "int")
        self.assertLess(r.confidence, 1.0)


class TestHoldoutValidation(unittest.TestCase):
    def test_pass_rate_reported(self):
        r = infer_column(col("int", 400), "v")
        self.assertIsNotNone(r.holdout_pass_rate)
        self.assertGreaterEqual(r.holdout_pass_rate, 0.95)

    def test_validation_failure_flagged(self):
        # 构造极端列：训练段（洗牌前头部）与留出段类型不同。
        # 通过直接调低阈值模拟：分布 60% int / 40% string，promote 为 string 后
        # 留出集全部可解析为 string，不会失败；改用 reject 外的确定性场景：
        # 前 100 个是 int，后 100 个是 float，且强制不留出 -> 不可行。
        # 正确做法：monkeypatch 洗牌为恒等，使留出段全是少数派。
        import typeinfer
        original = random.Random.shuffle
        random.Random.shuffle = lambda self, x: None  # 不打乱
        try:
            vals = col("string", 100, 8) + col("int", 300, 7)
            r = infer_column(vals, "vf")
        finally:
            random.Random.shuffle = original
        # 留出段(25%)全是 string，主类型 int 解析失败 -> validation_failed
        self.assertEqual(r.status, "validation_failed")
        self.assertLess(r.holdout_pass_rate, 0.95)


class TestWilson(unittest.TestCase):
    def test_small_sample_is_punished(self):
        self.assertLess(wilson_lower_bound(3, 3), 0.95)   # 3/3 也不敢下结论
        self.assertGreater(wilson_lower_bound(475, 500), 0.92)
        self.assertEqual(wilson_lower_bound(0, 0), 0.0)


class TestCsvSampling(unittest.TestCase):
    def test_not_only_head_rows(self):
        # 前 100 行是 int，后 900 行是文本：只看前几行会错判为 int。
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["c"])
                for i in range(100):
                    w.writerow([i])
                for i in range(900):
                    w.writerow([f"text_{i}"])
            res = infer_csv(path, sample_rows=200, seed=1)
            r = res["c"]
            self.assertEqual(r.total_rows, 1000)
            # 均匀抽样下训练集约 3/4 是 string
            self.assertEqual(r.type, "string" if r.status == "ok" else r.type)
            self.assertIn(r.status, ("ok", "mixed"))
            if r.status == "ok":
                self.assertEqual(r.type, "string")
        finally:
            os.unlink(path)

    def test_csv_end_to_end(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["i", "s", "e"])
                for k in range(200):
                    w.writerow([k, f"name_{k}", ""])
            res = infer_csv(path, sample_rows=500)
            self.assertEqual(res["i"].type, "int")
            self.assertEqual(res["s"].type, "string")
            self.assertEqual(res["e"].status, "empty")
        finally:
            os.unlink(path)


class TestBatch(unittest.TestCase):
    def test_infer_columns(self):
        res = infer_columns({
            "a": col("int", 100),
            "b": col("string", 100),
            "c": ["", ""] * 50,
        })
        self.assertEqual(res["a"].type, "int")
        self.assertEqual(res["b"].type, "string")
        self.assertEqual(res["c"].status, "empty")


if __name__ == "__main__":
    unittest.main(verbosity=2)
