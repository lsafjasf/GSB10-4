"""cost_calibrator 的自测（仅标准库 unittest）。

运行：python3 -m unittest -v
"""

import random
import unittest

from cost_calibrator import (
    CalibrationRejected,
    CostModel,
    DEGENERATE_FEATURES,
    DISTRIBUTION_DRIFT,
    INSUFFICIENT_SAMPLES,
    SINGULAR_MATRIX,
    calibrate,
    compute_metrics,
)

TRUE_COEF = [5.0, 1.0, 8.0, 0.02, 0.05]
BASELINE = [10.0, 2.0, 4.0, 0.05, 0.01]  # 拍脑袋系数


def make_samples(n, seed=7, feature_shift=None, noise_sigma=0.3):
    """生成合成样本；feature_shift 不为 None 时，后半段特征整体平移（模拟漂移）。"""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        shift = feature_shift if (feature_shift is not None and i >= n // 2) else 0.0
        x = (rng.lognormvariate(3.0 + shift, 1.0),
             rng.lognormvariate(1.0 + shift, 1.2),
             rng.lognormvariate(6.0 + shift, 1.0),
             rng.lognormvariate(3.0 + shift, 1.0))
        noise = rng.lognormvariate(0.0, noise_sigma)
        y = (TRUE_COEF[0] + sum(c * v for c, v in zip(TRUE_COEF[1:], x))) * noise
        out.append((x, y))
    return out


class SufficientSamplesTest(unittest.TestCase):
    """样本充足：校准应成功，且留出集误差显著下降。"""

    @classmethod
    def setUpClass(cls):
        cls.report = calibrate(make_samples(20_000), BASELINE, seed=42)

    def test_calibration_succeeds(self):
        self.assertEqual(len(self.report.coefficients), 5)
        self.assertGreater(self.report.n_holdout, 0)

    def test_holdout_error_improves(self):
        b = self.report.baseline_metrics
        c = self.report.calibrated_metrics
        self.assertLess(c.mae, b.mae * 0.7)
        self.assertLess(c.rmse, b.rmse * 0.7)
        self.assertLess(c.mean_qerror, b.mean_qerror)
        self.assertLess(c.p95_qerror, b.p95_qerror)

    def test_coefficients_close_to_truth(self):
        # 乘性噪声下 OLS 收敛到 E[noise] * 真值（约 1.05 倍），给 30% 容差；
        # 截距量级远小于其他项、估计方差大，单独放宽。
        for got, want in zip(self.report.coefficients[1:], TRUE_COEF[1:]):
            self.assertAlmostEqual(got, want, delta=max(0.3 * abs(want), 0.01))
        self.assertAlmostEqual(self.report.coefficients[0], TRUE_COEF[0],
                               delta=6.0)

    def test_metrics_are_on_holdout_only(self):
        self.assertEqual(self.report.n_train + self.report.n_holdout,
                         self.report.n_samples)
        self.assertEqual(self.report.baseline_metrics.n, self.report.n_holdout)
        self.assertEqual(self.report.calibrated_metrics.n, self.report.n_holdout)


class InsufficientSamplesTest(unittest.TestCase):
    """样本极少：必须拒绝，不得硬拟合。"""

    def test_tiny_sample_rejected(self):
        with self.assertRaises(CalibrationRejected) as ctx:
            calibrate(make_samples(30), BASELINE, seed=0)
        self.assertEqual(ctx.exception.reason, INSUFFICIENT_SAMPLES)

    def test_empty_input_rejected(self):
        with self.assertRaises(CalibrationRejected) as ctx:
            calibrate([], BASELINE, seed=0)
        self.assertEqual(ctx.exception.reason, INSUFFICIENT_SAMPLES)

    def test_boundary_just_below_minimum(self):
        with self.assertRaises(CalibrationRejected):
            calibrate(make_samples(99), BASELINE, seed=0, min_samples=100)

    def test_boundary_at_minimum_accepted(self):
        report = calibrate(make_samples(100), BASELINE, seed=0, min_samples=100)
        self.assertEqual(report.n_samples, 100)


class DistributionDriftTest(unittest.TestCase):
    """分布漂移：必须拒绝。"""

    def test_drifted_workload_rejected(self):
        samples = make_samples(20_000, feature_shift=3.0)
        with self.assertRaises(CalibrationRejected) as ctx:
            calibrate(samples, BASELINE, seed=1)
        self.assertEqual(ctx.exception.reason, DISTRIBUTION_DRIFT)

    def test_stable_workload_not_flagged(self):
        report = calibrate(make_samples(20_000), BASELINE, seed=1)
        self.assertTrue(all(p > 1e-3 for p in report.drift_pvalues))


class DegenerateDataTest(unittest.TestCase):
    """全为同一取值 / 共线：必须拒绝。"""

    def test_constant_features_rejected(self):
        rng = random.Random(3)
        samples = [((100.0, 5.0, 1000.0, 50.0), 42.0 + rng.random())
                   for _ in range(500)]
        with self.assertRaises(CalibrationRejected) as ctx:
            calibrate(samples, BASELINE, seed=0)
        self.assertEqual(ctx.exception.reason, DEGENERATE_FEATURES)

    def test_single_constant_feature_rejected(self):
        samples = [(x[0][:3] + (7.0,), x[1]) for x in make_samples(2_000)]
        with self.assertRaises(CalibrationRejected) as ctx:
            calibrate(samples, BASELINE, seed=0)
        self.assertEqual(ctx.exception.reason, DEGENERATE_FEATURES)

    def test_collinear_features_rejected(self):
        samples = [((x[0][0], 2.0 * x[0][0], x[0][2], x[0][3]), x[1])
                   for x in make_samples(2_000)]
        with self.assertRaises(CalibrationRejected) as ctx:
            calibrate(samples, BASELINE, seed=0)
        self.assertEqual(ctx.exception.reason, SINGULAR_MATRIX)


class ReproducibilityTest(unittest.TestCase):
    """可复现：同输入同种子，系数逐位一致。"""

    def test_same_seed_identical_coefficients(self):
        data = make_samples(10_000)
        r1 = calibrate(iter(data), BASELINE, seed=123)
        r2 = calibrate(iter(data), BASELINE, seed=123)
        self.assertEqual(r1.coefficients, r2.coefficients)  # 完全相等，非近似
        self.assertEqual(r1.n_train, r2.n_train)

    def test_generator_input_reproducible(self):
        # 流式输入（生成器）与列表输入结果一致
        data = make_samples(5_000)
        r1 = calibrate(data, BASELINE, seed=9)
        r2 = calibrate(iter(data), BASELINE, seed=9)
        self.assertEqual(r1.coefficients, r2.coefficients)

    def test_different_seed_differs(self):
        data = make_samples(10_000)
        r1 = calibrate(data, BASELINE, seed=1)
        r2 = calibrate(data, BASELINE, seed=2)
        self.assertNotEqual(r1.coefficients, r2.coefficients)


class MetricsTest(unittest.TestCase):
    """指标计算的基本正确性。"""

    def test_perfect_prediction(self):
        model = CostModel([1.0, 2.0])
        from array import array
        feats = array("d", [3.0, 4.0, 5.0])
        targets = array("d", [7.0, 9.0, 11.0])
        m = compute_metrics(model, feats, targets, 1)
        self.assertAlmostEqual(m.mae, 0.0)
        self.assertAlmostEqual(m.rmse, 0.0)
        self.assertAlmostEqual(m.mean_qerror, 1.0)

    def test_qerror_symmetry(self):
        model_over = CostModel([0.0, 2.0])
        model_under = CostModel([0.0, 0.5])
        from array import array
        feats = array("d", [10.0])
        targets = array("d", [10.0])
        m_over = compute_metrics(model_over, feats, targets, 1)
        m_under = compute_metrics(model_under, feats, targets, 1)
        self.assertAlmostEqual(m_over.mean_qerror, 2.0)
        self.assertAlmostEqual(m_under.mean_qerror, 2.0)


if __name__ == "__main__":
    unittest.main()
