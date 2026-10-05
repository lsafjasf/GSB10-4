"""freivalds 库的自测：python3 -m unittest -v

覆盖：确定输入（永不错杀）、构造性坏输入、随机源退化、目标错误率极严、
两份实现对拍、同种子复现、重复次数公式。
"""

import math
import unittest

import freivalds as F


def eye(n):
    return [[1 if i == j else 0 for j in range(n)] for i in range(n)]


def flip(mat, i, j):
    m = [row[:] for row in mat]
    m[i][j] ^= 1
    return m


def rank1_witness(n):
    """构造最坏情形：AB 与 C 只差一个元素（D 秩 1），单次误判率恰为 1/2。"""
    a = eye(n)
    b = eye(n)
    c = flip(eye(n), 0, 0)
    return a, b, c


class TestTrialsNeeded(unittest.TestCase):
    def test_formula(self):
        self.assertEqual(F.trials_needed(0.5), 1)
        self.assertEqual(F.trials_needed(0.05), 5)      # 2^-5 = 0.03125
        self.assertEqual(F.trials_needed(1e-6), 20)     # 2^-20 ≈ 9.54e-7
        self.assertEqual(F.trials_needed(1e-12), 40)
        self.assertEqual(F.trials_needed(1e-18), 60)
        self.assertEqual(F.trials_needed(1e-300), 997)

    def test_bound_holds(self):
        for eps in (0.49, 0.1, 1e-3, 1e-12, 1e-100):
            k = F.trials_needed(eps)
            self.assertLessEqual(2.0 ** -k, eps)
            self.assertGreater(2.0 ** -(k - 1), eps)  # 最小性

    def test_invalid(self):
        for bad in (0.0, 1.0, -0.1, 2.0):
            with self.assertRaises(ValueError):
                F.trials_needed(bad)


class TestDeterministicInputs(unittest.TestCase):
    """确定输入（AB == C）：任何种子、任何 k 都必须 ACCEPT，假阳性率为 0。"""

    def test_correct_always_accept(self):
        for n in (1, 2, 3, 8, 16):
            a = F.gf2_matmul(eye(n), eye(n))
            b = eye(n)
            c = F.gf2_matmul(a, b)
            for seed in range(20):
                self.assertTrue(
                    F.verify_packed(
                        F.BitMatrix.from_rows(a),
                        F.BitMatrix.from_rows(b),
                        F.BitMatrix.from_rows(c),
                        F.SeededSource(seed),
                        k=8,
                    )
                )

    def test_zero_matrix(self):
        n = 4
        z = [[0] * n for _ in range(n)]
        src = F.SeededSource(7)
        self.assertTrue(
            F.verify_packed(
                F.BitMatrix.from_rows(z),
                F.BitMatrix.from_rows(z),
                F.BitMatrix.from_rows(z),
                src,
                k=10,
            )
        )


class TestAdversarialInputs(unittest.TestCase):
    """构造性坏输入：D = AB⊕C 秩 1 时单次误判率恰为 1/2（上界取等）。"""

    def test_rank1_single_trial_rate(self):
        n = 8
        a, b, c = rank1_witness(n)
        pa, pb, pc = (F.BitMatrix.from_rows(m) for m in (a, b, c))
        src = F.SeededSource(12345)
        trials = 20000
        accepts = sum(
            F.freivalds_once_packed(pa, pb, pc, src) for _ in range(trials)
        )
        p_hat = accepts / trials
        # 理论恰为 1/2；4 个标准误 ≈ 0.014
        self.assertAlmostEqual(p_hat, 0.5, delta=0.02)

    def test_rank1_repeated_power_decay(self):
        n = 8
        a, b, c = rank1_witness(n)
        pa, pb, pc = (F.BitMatrix.from_rows(m) for m in (a, b, c))
        src = F.SeededSource(999)
        cases = 4000
        for k in (1, 2, 4, 6):
            fails = sum(
                F.verify_packed(pa, pb, pc, src, k, check=False)
                for _ in range(cases)
            )
            p_hat = fails / cases
            # 观测失败率应 ≈ 2^-k；容差取 5 个标准误
            se = math.sqrt(2.0 ** -k * (1 - 2.0 ** -k) / cases)
            self.assertAlmostEqual(p_hat, 2.0 ** -k, delta=max(5 * se, 0.01))

    def test_full_rank_d_never_accepted(self):
        """D 满秩时单次误判率也是 1/2，但这里验证 k 足够大时基本全 REJECT。"""
        n = 6
        a, b = eye(n), eye(n)
        c = flip(eye(n), 0, 0)
        # 再翻转让 D 更“稠密”
        for i in range(n):
            c = flip(c, i, (i + 1) % n)
        src = F.SeededSource(5)
        for _ in range(200):
            self.assertFalse(
                F.verify_packed(
                    F.BitMatrix.from_rows(a),
                    F.BitMatrix.from_rows(b),
                    F.BitMatrix.from_rows(c),
                    src,
                    k=10,
                    check=False,
                )
            )


class TestDegenerateSources(unittest.TestCase):
    def test_constant_bit_source_detected(self):
        n = 4
        a, b, c = rank1_witness(n)
        pa, pb, pc = (F.BitMatrix.from_rows(m) for m in (a, b, c))
        for value in (0, 1):
            with self.assertRaises(F.DegenerateRandomnessError):
                F.verify_packed(pa, pb, pc, F.ConstantBitSource(value), k=5)

    def test_fixed_vector_source_detected(self):
        n = 4
        a, b, c = rank1_witness(n)
        pa, pb, pc = (F.BitMatrix.from_rows(m) for m in (a, b, c))
        with self.assertRaises(F.DegenerateRandomnessError):
            F.verify_packed(pa, pb, pc, F.FixedVectorSource([1, 0, 1, 1]), k=5)

    def test_degenerate_source_actually_breaks_guarantee(self):
        """演示：若不做健康检查，固定向量源会让 k 次重复完全失效。"""
        n = 4
        a, b, c = rank1_witness(n)
        pa, pb, pc = (F.BitMatrix.from_rows(m) for m in (a, b, c))
        # 找一个使单次 ACCEPT 的向量 r（Dr=0），固定它重复 100 次仍 ACCEPT
        r = [0, 1, 0, 0]  # D = e1·e1^T，Dr = 0
        fooled = F.verify_packed(
            pa, pb, pc, F.FixedVectorSource(r), k=100, check=False
        )
        self.assertTrue(fooled)  # 被欺骗：说明健康检查是必要的

    def test_healthy_source_passes_check(self):
        F.health_check(F.SeededSource(42), n=8)


class TestReproducibility(unittest.TestCase):
    def test_same_seed_same_stream(self):
        s1, s2 = F.SeededSource(2026), F.SeededSource(2026)
        self.assertEqual(
            [s1.random_bit() for _ in range(1000)],
            [s2.random_bit() for _ in range(1000)],
        )

    def test_same_seed_same_verdict(self):
        n = 8
        a, b, c = rank1_witness(n)
        pa, pb, pc = (F.BitMatrix.from_rows(m) for m in (a, b, c))
        v1 = [F.verify_packed(pa, pb, pc, F.SeededSource(s), k=10) for s in range(50)]
        v2 = [F.verify_packed(pa, pb, pc, F.SeededSource(s), k=10) for s in range(50)]
        self.assertEqual(v1, v2)


class TestDifferential(unittest.TestCase):
    """朴素实现 vs 位打包实现对拍：同一随机源序列下判定必须完全一致。"""

    def test_naive_vs_packed(self):
        rng = F.SeededSource(31337)
        for case in range(300):
            n = 1 + case % 9
            a = [[rng.random_bit() for _ in range(n)] for _ in range(n)]
            b = [[rng.random_bit() for _ in range(n)] for _ in range(n)]
            c = F.gf2_matmul(a, b)
            if case % 3 == 0:  # 1/3 注入错误
                c = flip(c, case % n, (case * 7) % n)
            pa, pb, pc = (F.BitMatrix.from_rows(m) for m in (a, b, c))
            for seed in (case, case + 1000):
                k = 1 + (case % 5)
                vn = F.verify_naive(a, b, c, F.SeededSource(seed), k)
                vp = F.verify_packed(pa, pb, pc, F.SeededSource(seed), k)
                self.assertEqual(vn, vp, f"case={case} seed={seed}")

    def test_packed_matmul_matches_naive(self):
        rng = F.SeededSource(1)
        for n in (1, 2, 5, 16, 33):
            a = [[rng.random_bit() for _ in range(n)] for _ in range(n)]
            b = [[rng.random_bit() for _ in range(n)] for _ in range(n)]
            expect = F.gf2_matmul(a, b)
            got = F.BitMatrix.from_rows(a).matmul(F.BitMatrix.from_rows(b))
            self.assertEqual(F.BitMatrix.from_rows(expect), got)


if __name__ == "__main__":
    unittest.main()
