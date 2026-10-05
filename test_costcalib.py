"""costcalib 自测：python3 -m unittest test_costcalib -v"""
import math
import random
import unittest

import costcalib
from costcalib import calibrate

TRUE_W = [3.0, 0.02, 1.5, -0.7, 0.001]   # 截距 + 4 个特征
BAD_W = [10.0, 0.05, 0.5, 0.5, 0.01]     # “拍出来的”初始系数


def gen_samples(n, w=TRUE_W, seed=42, noise=0.5, shift_at=None, shift=0.0,
                constant_features=None):
    """生成 (features, actual) 列表；shift_at 之后特征整体平移，模拟分布漂移。"""
    rng = random.Random(seed)
    constant_features = constant_features or set()
    out = []
    for i in range(n):
        x = []
        for j in range(len(w) - 1):
            if j in constant_features:
                v = 7.0
            else:
                v = rng.uniform(0.0, 100.0) + (shift if shift_at is not None and i >= shift_at else 0.0)
            x.append(v)
        y = w[0] + sum(w[j + 1] * x[j] for j in range(len(x))) + rng.gauss(0.0, noise)
        out.append((x, y))
    return out


class TestSufficientSamples(unittest.TestCase):
    def test_calibrates_and_improves(self):
        samples = gen_samples(30000)
        r = calibrate(samples, BAD_W, seed=1)
        self.assertEqual(r.status, "calibrated")
        self.assertLess(r.after.rmse, r.before.rmse)
        self.assertGreater(r.after.r2, r.before.r2)
        # 拟合系数应接近真值
        for got, want in zip(r.coefficients, TRUE_W):
            self.assertAlmostEqual(got, want, delta=max(0.05, abs(want) * 0.05))
        # 留出集确实被保留
        self.assertGreater(r.n_holdout, 1000)
        self.assertGreater(r.n_train, 1000)

    def test_metrics_reported(self):
        r = calibrate(gen_samples(20000), BAD_W, seed=2)
        self.assertIsNotNone(r.before)
        self.assertIsNotNone(r.after)
        for m in (r.before, r.after):
            self.assertGreaterEqual(m.mae, 0.0)
            self.assertGreaterEqual(m.rmse, 0.0)
            self.assertGreaterEqual(m.smape, 0.0)


class TestInsufficientSamples(unittest.TestCase):
    def test_too_few_samples_rejected(self):
        r = calibrate(gen_samples(50), BAD_W, seed=3)
        self.assertEqual(r.status, "rejected")
        self.assertEqual(r.reason, costcalib.REASON_INSUFFICIENT)
        self.assertEqual(tuple(BAD_W), r.coefficients)  # 保留原系数

    def test_empty_input_rejected(self):
        r = calibrate([], BAD_W, seed=3)
        self.assertEqual(r.status, "rejected")
        self.assertEqual(r.reason, costcalib.REASON_INSUFFICIENT)


class TestDistributionDrift(unittest.TestCase):
    def test_drift_rejected(self):
        # 后半段特征分布整体平移 +200，PSI 必然爆表
        samples = gen_samples(20000, shift_at=10000, shift=200.0)
        r = calibrate(samples, BAD_W, seed=4)
        self.assertEqual(r.status, "rejected")
        self.assertEqual(r.reason, costcalib.REASON_DRIFT)
        self.assertGreaterEqual(r.psi_max, 0.25)
        self.assertEqual(tuple(BAD_W), r.coefficients)

    def test_stable_distribution_accepted(self):
        r = calibrate(gen_samples(20000), BAD_W, seed=4)
        self.assertEqual(r.status, "calibrated")
        self.assertLess(r.psi_max, 0.25)


class TestConstantFeatures(unittest.TestCase):
    def test_all_constant_rejected(self):
        samples = [([5.0, 5.0, 5.0, 5.0], 42.0) for _ in range(5000)]
        r = calibrate(samples, BAD_W, seed=5)
        self.assertEqual(r.status, "rejected")
        self.assertEqual(r.reason, costcalib.REASON_DEGENERATE)

    def test_partial_constant_frozen(self):
        # 只有特征 2 是常量：其系数保持原值，其余正常拟合
        samples = gen_samples(20000, constant_features={1})
        r = calibrate(samples, BAD_W, seed=6)
        self.assertEqual(r.status, "calibrated")
        self.assertEqual(r.coefficients[2], BAD_W[2])
        # 冻结系数的偏差 (1.5-0.5)*7.0 被截距吸收
        self.assertAlmostEqual(r.coefficients[0], TRUE_W[0] + 7.0, delta=0.15)
        for pos in (1, 3, 4):
            self.assertAlmostEqual(r.coefficients[pos], TRUE_W[pos],
                                   delta=max(0.05, abs(TRUE_W[pos]) * 0.05))


class TestReproducibility(unittest.TestCase):
    def test_same_input_same_seed_identical(self):
        samples = gen_samples(20000)
        r1 = calibrate(samples, BAD_W, seed=7)
        r2 = calibrate(list(samples), BAD_W, seed=7)
        self.assertEqual(r1.coefficients, r2.coefficients)  # 完全一致，非近似
        self.assertEqual(r1.after, r2.after)

    def test_generator_input_reproducible(self):
        # 流式输入（生成器）两次运行结果也必须一致
        r1 = calibrate((s for s in gen_samples(15000)), BAD_W, seed=8)
        r2 = calibrate((s for s in gen_samples(15000)), BAD_W, seed=8)
        self.assertEqual(r1.coefficients, r2.coefficients)


class TestNoImprovement(unittest.TestCase):
    def test_pure_noise_rejected(self):
        # 目标与特征完全无关：拟合结果在留出集上不会更好，必须拒绝
        rng = random.Random(9)
        samples = [([rng.uniform(0, 100) for _ in range(4)], rng.gauss(50, 10))
                   for _ in range(20000)]
        r = calibrate(samples, [50.0, 0.0, 0.0, 0.0, 0.0], seed=9)
        self.assertEqual(r.status, "rejected")
        self.assertEqual(r.reason, costcalib.REASON_NO_IMPROVEMENT)
        self.assertIsNotNone(r.before)
        self.assertIsNotNone(r.after)
        self.assertGreaterEqual(r.after.rmse, r.before.rmse)


class TestValidation(unittest.TestCase):
    def test_dimension_mismatch_raises(self):
        with self.assertRaises(ValueError):
            list(calibrate([([1.0, 2.0], 3.0)], BAD_W))

    def test_calibrate_or_raise(self):
        with self.assertRaises(costcalib.CalibrationRejected):
            costcalib.calibrate_or_raise(gen_samples(10), BAD_W)
        w = costcalib.calibrate_or_raise(gen_samples(20000), BAD_W, seed=11)
        self.assertEqual(len(w), len(BAD_W))


if __name__ == "__main__":
    unittest.main()
