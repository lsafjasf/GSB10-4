"""开放地址法哈希表自测：正确性、边界情形、与链地址法对拍。

运行: python3 test_oaht.py -v
"""
import random
import unittest

from oaht import OpenAddressingHashTable
from chaining import ChainingHashTable


class TestBasic(unittest.TestCase):
    def test_put_get_delete(self):
        t = OpenAddressingHashTable()
        self.assertTrue(t.put("a", 1))
        self.assertFalse(t.put("a", 2))          # 覆盖
        self.assertEqual(t.get("a"), 2)
        self.assertEqual(len(t), 1)
        self.assertTrue(t.delete("a"))
        self.assertFalse(t.delete("a"))          # 重复删除
        self.assertIsNone(t.get("a"))
        self.assertEqual(len(t), 0)

    def test_dict_like_interface(self):
        t = OpenAddressingHashTable()
        t["x"] = 10
        self.assertEqual(t["x"], 10)
        self.assertIn("x", t)
        self.assertNotIn("y", t)
        with self.assertRaises(KeyError):
            _ = t["y"]

    def test_random_against_dict(self):
        """低负载随机操作，与内置 dict 对拍。"""
        rng = random.Random(1)
        t = OpenAddressingHashTable()
        ref = {}
        for _ in range(50_000):
            k = rng.randrange(10_000)
            op = rng.random()
            if op < 0.5:
                t.put(k, k + 1); ref[k] = k + 1
            elif op < 0.8:
                self.assertEqual(t.get(k), ref.get(k))
            else:
                self.assertEqual(t.delete(k), ref.pop(k, None) is not None)
        self.assertEqual(len(t), len(ref))
        self.assertEqual(dict(t.items()), ref)


class TestTombstone(unittest.TestCase):
    def test_delete_does_not_break_probe_chain(self):
        """删掉链中间的键后，链后面的键必须还能找到。"""
        t = OpenAddressingHashTable(capacity=8, hash_fn=lambda k: 0)  # 全部同槽
        for i in range(4):                       # 形成一条探测链
            t.put(i, i)
        t.delete(1)                              # 删链中间
        t.delete(2)
        self.assertEqual(t.get(3), 3)            # 链尾仍可查
        self.assertIsNone(t.get(1))
        self.assertIsNone(t.get(2))

    def test_reinsert_reuses_tombstone(self):
        t = OpenAddressingHashTable(capacity=8, hash_fn=lambda k: 0)
        t.put(1, 1); t.put(2, 2)
        t.delete(1)
        tomb_before = t.tombstones
        t.put(3, 3)                              # 应复用墓碑槽
        self.assertEqual(t.tombstones, tomb_before - 1)
        self.assertEqual(t.get(3), 3)

    def test_mass_delete_no_degradation(self):
        """连续删除后：未命中查找探长必须被墓碑清理机制压住。"""
        n = 200_000
        t = OpenAddressingHashTable(collect_stats=True)
        for i in range(n):
            t.put(i, i)
        for i in range(n - n // 10):             # 删除 90%
            t.delete(i)
        t.probe_hist.clear(); t.max_probe = 0
        for i in range(n - n // 10, n):          # 命中查找
            self.assertEqual(t.get(i), i)
        hit_max = t.max_probe
        t.probe_hist.clear(); t.max_probe = 0
        for i in range(n, n + 10_000):           # 未命中查找
            self.assertIsNone(t.get(i))
        miss_max = t.max_probe
        # 双重哈希 + 负载<=0.7 时，未命中期望探长 ~3.3；
        # 若墓碑失控这里会涨到成百上千。给足余量断言。
        self.assertLessEqual(hit_max, 64)
        self.assertLessEqual(miss_max, 64)
        self.assertLessEqual(t.tombstones, max(t.capacity // 8, len(t)))


class TestLoadBound(unittest.TestCase):
    def test_load_factor_never_exceeds_bound(self):
        max_load = 0.7
        t = OpenAddressingHashTable(max_load=max_load)
        peak = 0.0
        for i in range(300_000):
            t.put(i, i)
            peak = max(peak, (len(t) + t.tombstones) / t.capacity)
        self.assertLessEqual(peak, max_load + 1e-9)
        self.assertGreater(t.capacity, 300_000)  # 确实扩过容

    def test_near_full_table_correct(self):
        """逼近上界：把表填到再散列临界点，验证全部可查。"""
        t = OpenAddressingHashTable(capacity=8)
        i = 0
        # 找到刚好不再扩容的最大填充量（cap=8 时阈值 0.7*8=5）
        while (len(t) + 1 + t.tombstones) <= 0.7 * t.capacity:
            t.put(i, i); i += 1
        self.assertGreaterEqual(len(t), 5)
        for k in range(len(t)):
            self.assertEqual(t.get(k), k)


class TestDegenerateHash(unittest.TestCase):
    def test_constant_hash_still_correct(self):
        """哈希函数完全退化（所有键同槽）时退化为线性探测，正确性必须保持。"""
        t = OpenAddressingHashTable(hash_fn=lambda k: 0)
        ref = {}
        rng = random.Random(7)
        for _ in range(20_000):
            k = rng.randrange(2_000)
            if rng.random() < 0.6:
                t.put(k, k); ref[k] = k
            else:
                self.assertEqual(t.delete(k), ref.pop(k, None) is not None)
        self.assertEqual(dict(t.items()), ref)
        for k in range(2_000):
            self.assertEqual(t.get(k), ref.get(k))

    def test_low_entropy_hash(self):
        """哈希只有 4 个不同取值。"""
        t = OpenAddressingHashTable(hash_fn=lambda k: k % 4)
        for i in range(5_000):
            t.put(i, i)
        for i in range(5_000):
            self.assertEqual(t.get(i), i)


class TestCrossCheck(unittest.TestCase):
    def test_open_addressing_vs_chaining_same_ops(self):
        """同一批键、同一操作序列：两种实现结果必须逐一一致。"""
        rng = random.Random(20261005)
        oa = OpenAddressingHashTable()
        ch = ChainingHashTable()
        ref = {}
        gets = 0
        for _ in range(300_000):
            k = rng.randrange(50_000)            # 键空间小，碰撞/删除频繁
            r = rng.random()
            if r < 0.45:
                v = rng.randrange(1 << 30)
                self.assertEqual(oa.put(k, v), ch.put(k, v))
                ref[k] = v
            elif r < 0.75:
                g_oa, g_ch = oa.get(k, -1), ch.get(k, -1)
                self.assertEqual(g_oa, g_ch)
                self.assertEqual(g_oa, ref.get(k, -1))
                gets += 1
            else:
                self.assertEqual(oa.delete(k), ch.delete(k))
                ref.pop(k, None)
        self.assertGreater(gets, 50_000)
        self.assertEqual(len(oa), len(ch))
        self.assertEqual(dict(oa.items()), dict(ch.items()))
        self.assertEqual(dict(oa.items()), ref)


if __name__ == "__main__":
    unittest.main()
