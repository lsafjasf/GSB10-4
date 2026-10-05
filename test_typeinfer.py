"""typeinfer 自测：纯数字 / 纯文本 / 混合类型 / 整列为空 / 样本不足 / 抽样公正性等。"""

import io
import random
import unittest

from typeinfer import (
    DECISION_PROMOTE, DECISION_PROMOTE_TEXT, DECISION_REJECT,
    STATUS_EMPTY, STATUS_INFERRED, STATUS_INSUFFICIENT, STATUS_REJECTED,
    TypeInferencer, infer_csv,
)


def run_columns(cols, **kwargs):
    """把若干列（等长或不等长的字符串列表）喂给推断器，返回推断结果列表。"""
    kwargs.setdefault("seed", 42)
    inf = TypeInferencer(len(cols), [f"c{i}" for i in range(len(cols))], **kwargs)
    n_rows = max(len(c) for c in cols)
    for i in range(n_rows):
        inf.add_row([c[i] if i < len(c) else "" for c in cols])
    return inf.infer()


def gen_ints(n, start=0):
    return [str(start + i) for i in range(n)]


class PureTypeTests(unittest.TestCase):
    def test_pure_int(self):
        [r] = run_columns([gen_ints(5000)])
        self.assertEqual(r.status, STATUS_INFERRED)
        self.assertEqual(r.type, "int")
        self.assertEqual(r.distribution, {"int": r.sample_size})
        self.assertGreater(r.confidence, 0.9)
        self.assertEqual(r.holdout_pass_rate, 1.0)
        self.assertGreater(r.holdout_size, 0)

    def test_pure_float(self):
        [r] = run_columns([[f"{i}.{i % 10}" for i in range(5000)]])
        self.assertEqual(r.status, STATUS_INFERRED)
        self.assertEqual(r.type, "float")
        self.assertEqual(r.holdout_pass_rate, 1.0)

    def test_float_formats(self):
        vals = ["1e5", ".5", "3.", "-2.5e-3", "+4", "0"] * 100
        [r] = run_columns([vals])
        self.assertEqual(r.type, "float")  # int 值提升为 float
        self.assertEqual(r.decision, DECISION_PROMOTE)

    def test_pure_text(self):
        rng = random.Random(1)
        vals = [f"name_{rng.randrange(10**6)}_x" for _ in range(5000)]
        [r] = run_columns([vals])
        self.assertEqual(r.status, STATUS_INFERRED)
        self.assertEqual(r.type, "text")
        self.assertIsNone(r.decision)

    def test_pure_bool(self):
        vals = ["true", "false", "yes", "no", "True", "NO"] * 1000
        [r] = run_columns([vals])
        self.assertEqual(r.type, "bool")
        self.assertEqual(r.holdout_pass_rate, 1.0)

    def test_zero_one_is_int_not_bool(self):
        [r] = run_columns([["0", "1"] * 2500])
        self.assertEqual(r.type, "int")

    def test_big_int_stays_int(self):
        [r] = run_columns([[str(2**60 + i) for i in range(2000)]])
        self.assertEqual(r.type, "int")


class MixedTypeTests(unittest.TestCase):
    def test_int_float_promotes_to_float(self):
        vals = ["1", "2.5", "3", "4.25", "1e3"] * 1000
        [r] = run_columns([vals])
        self.assertEqual(r.status, STATUS_INFERRED)
        self.assertEqual(r.type, "float")
        self.assertEqual(r.decision, DECISION_PROMOTE)
        self.assertIn("float", r.cost)
        self.assertEqual(r.holdout_pass_rate, 1.0)

    def test_bool_int_promotes_to_int(self):
        vals = ["true", "0", "false", "1", "7"] * 1000
        [r] = run_columns([vals])
        self.assertEqual(r.type, "int")
        self.assertEqual(r.decision, DECISION_PROMOTE)

    def test_rare_text_promotes_to_text(self):
        # 1% 文本混入 -> 确定性整体提升为文本（无损），并说明代价
        vals = gen_ints(4950) + ["oops"] * 50
        [r] = run_columns([vals])
        self.assertEqual(r.status, STATUS_INFERRED)
        self.assertEqual(r.type, "text")
        self.assertEqual(r.decision, DECISION_PROMOTE_TEXT)
        self.assertIn("失去数值", r.cost)
        self.assertEqual(r.holdout_pass_rate, 1.0)

    def test_heavily_mixed_rejected(self):
        # 60% int + 40% text：超过容忍度又不到文本主导 -> 拒绝
        vals = gen_ints(3000) + [f"junk_{i}" for i in range(2000)]
        [r] = run_columns([vals])
        self.assertEqual(r.status, STATUS_REJECTED)
        self.assertIsNone(r.type)
        self.assertEqual(r.decision, DECISION_REJECT)
        self.assertIn("拆分", r.message)  # 给出拆分/提升两个备选及代价

    def test_text_dominant_is_text(self):
        vals = [f"v_{i}" for i in range(4000)] + gen_ints(1000)
        [r] = run_columns([vals])
        self.assertEqual(r.status, STATUS_INFERRED)
        self.assertEqual(r.type, "text")
        self.assertIsNone(r.decision)


class EdgeCaseTests(unittest.TestCase):
    def test_all_empty_column(self):
        [r] = run_columns([["", "NA", "null", "None", "nan"] * 1000])
        self.assertEqual(r.status, STATUS_EMPTY)
        self.assertIsNone(r.type)
        self.assertIn("整列为空", r.message)

    def test_insufficient_sample_reports_size(self):
        # 只有 10 个非空值，其余全空
        vals = gen_ints(10) + [""] * 4990
        [r] = run_columns([vals])
        self.assertEqual(r.status, STATUS_INSUFFICIENT)
        self.assertIsNone(r.type)
        self.assertLess(r.sample_size, 30)
        self.assertIn(str(r.sample_size), r.message)  # 报出实际样本量

    def test_tiny_column(self):
        [r] = run_columns([["1", "2", "3"]])
        self.assertEqual(r.status, STATUS_INSUFFICIENT)

    def test_sampling_not_just_first_rows(self):
        # 文本只出现在最后 1.5% 的行；若只看前几行会误判为纯 int
        vals = gen_ints(9850) + ["tail_text"] * 150
        [r] = run_columns([vals])
        self.assertGreater(r.distribution.get("text", 0), 0)  # 抽到了尾部文本
        self.assertEqual(r.decision, DECISION_PROMOTE_TEXT)
        self.assertEqual(r.type, "text")

    def test_null_rate_estimated(self):
        vals = [str(i) if i % 2 == 0 else "" for i in range(10000)]
        [r] = run_columns([vals])
        self.assertEqual(r.type, "int")
        self.assertAlmostEqual(r.null_rate, 0.5, delta=0.1)

    def test_ragged_rows(self):
        inf = TypeInferencer(3, ["a", "b", "c"], seed=1)
        inf.add_row(["1", "x", "true", "extra_ignored"])
        for _ in range(200):
            inf.add_row(["2"])  # 短行：b、c 按空值处理
        results = inf.infer()
        self.assertEqual(len(results), 3)
        self.assertGreater(results[1].null_rate, 0.5)

    def test_deterministic_with_seed(self):
        vals = gen_ints(3000) + ["x"] * 30
        r1 = run_columns([vals])[0]
        r2 = run_columns([vals])[0]
        self.assertEqual(r1.to_dict(), r2.to_dict())

    def test_holdout_validation_present(self):
        [r] = run_columns([gen_ints(5000)])
        self.assertGreaterEqual(r.holdout_size, 50)
        self.assertIsNotNone(r.holdout_pass_rate)

    def test_rows_seen_counted(self):
        [r] = run_columns([gen_ints(12345)])
        self.assertEqual(r.rows_seen, 12345)


class CsvAdapterTests(unittest.TestCase):
    def test_infer_csv_with_header(self):
        buf = io.StringIO("id,name,score\n1,alice,9.5\n2,bob,8.0\n")
        buf.seek(0, 2)  # 移到末尾再追加，避免覆盖表头
        for i in range(3, 200):
            buf.write(f"{i},user_{i},{i % 10}.5\n")
        buf.seek(0)
        results = infer_csv(buf)
        self.assertEqual([r.column for r in results], ["id", "name", "score"])
        self.assertEqual(results[0].type, "int")
        self.assertEqual(results[1].type, "text")
        self.assertEqual(results[2].type, "float")

    def test_infer_csv_empty(self):
        self.assertEqual(infer_csv(io.StringIO("")), [])


if __name__ == "__main__":
    unittest.main()
