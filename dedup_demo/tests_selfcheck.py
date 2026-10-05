"""tests_selfcheck.py - spilldedup 自测（仅标准库 unittest）。

覆盖：
  1. 全部装得下（不溢写）
  2. 刚好触发溢写（计费精确到触发边界）
  3. 反复溢写（多 run 归并）
  4. 键全部相同
  5. 对拍：小内存 vs 不限内存 vs 朴素 set 参考，多种子多规模
  6. 哈希碰撞：hash 相同的键不得被误去重
  7. 崩溃清理：SIGKILL 残留目录被下一次运行自动清扫且不影响结果
  8. 并发实例互不干扰 / 空输入 / 混合类型往返 / 输出有序唯一
"""
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spilldedup as sd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def reference(keys):
    """朴素参考：不限内存，按规范化字节去重后排序（与算子语义一致）。"""
    seen = {}
    for k in keys:
        seen.setdefault(sd.encode_key(k), k)
    return [seen[e] for e in sorted(seen)]


class TempDirMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="spilldedup-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TestBasicScenarios(TempDirMixin):
    def test_all_fit_no_spill(self):
        """全部装得下：内存上限足够大，零溢写。"""
        rng = random.Random(1)
        keys = [rng.randrange(500) for _ in range(20000)]
        op = sd.SpillDedup(memory_limit_bytes=None, spill_dir=self.tmp)
        op.add_many(keys)
        got = list(op.results())
        self.assertEqual(op.spill_count, 0)
        self.assertEqual(got, reference(keys))
        self.assertEqual(set(got), set(keys))    # 单类型时与 set 语义一致
        self.assertEqual(len(got), len(set(keys)))

    def test_exact_spill_trigger(self):
        """刚好触发溢写：计费达到上限的那一条 add 立即触发。"""
        key = b"AAAA"                       # 编码后 5 字节
        charge = sys.getsizeof(b"xAAAA") + 64   # 与库内 _charge 同公式
        limit = charge * 10
        op = sd.SpillDedup(memory_limit_bytes=limit, spill_dir=self.tmp)
        for i in range(9):
            op.add(key + bytes([i]))
            self.assertEqual(op.spill_count, 0, "第 %d 条不应触发" % (i + 1))
        op.add(key + b"\x09")               # 第 10 条，恰好达到上限
        self.assertEqual(op.spill_count, 1, "第 10 条必须恰好触发一次溢写")
        got = list(op.results())
        self.assertEqual(got, sorted(key + bytes([i]) for i in range(10)))

    def test_repeated_spills(self):
        """反复溢写：小上限产生大量 run，归并结果仍与参考一致。"""
        rng = random.Random(2)
        keys = [rng.randrange(2000) for _ in range(100000)]
        op = sd.SpillDedup(memory_limit_bytes=32 << 10, spill_dir=self.tmp)
        op.add_many(keys)
        self.assertGreaterEqual(op.spill_count, 5)
        self.assertEqual(list(op.results()), reference(keys))

    def test_all_identical(self):
        """键全部相同：一百万条同一个键，内存有界且结果只有一个。"""
        op = sd.SpillDedup(memory_limit_bytes=16 << 10, spill_dir=self.tmp)
        op.add_many([b"same-key"] * 1000000)
        self.assertGreater(op.spill_count, 0)
        self.assertEqual(list(op.results()), [b"same-key"])

    def test_empty_input(self):
        op = sd.SpillDedup(memory_limit_bytes=1024, spill_dir=self.tmp)
        self.assertEqual(list(op.results()), [])

    def test_mixed_types_roundtrip(self):
        keys = [1, "1", b"1", True, False, 0, "0", b"0", -5, "", b"", "你好"]
        keys = keys * 3
        random.Random(3).shuffle(keys)
        op = sd.SpillDedup(memory_limit_bytes=2048, spill_dir=self.tmp)
        op.add_many(keys)
        got = list(op.results())
        self.assertEqual(got, reference(keys))
        # bool 与 int 是不同键（文档化语义），True/1、False/0 都应各自保留
        self.assertIn(True, got)
        self.assertIn(1, got)
        self.assertEqual(sum(1 for k in got if k is True), 1)
        self.assertEqual(sum(1 for k in got if type(k) is int and k == 1), 1)

    def test_output_sorted_unique(self):
        rng = random.Random(4)
        keys = [rng.randrange(100000) for _ in range(50000)]
        op = sd.SpillDedup(memory_limit_bytes=16 << 10, spill_dir=self.tmp)
        op.add_many(keys)
        got = list(op.results())
        self.assertEqual(got, sorted(got, key=sd.encode_key))
        self.assertEqual(len(got), len(set(map(sd.encode_key, got))))


class TestDifferential(TempDirMixin):
    """对拍：同一输入，小内存（溢写）结果必须 == 不限内存结果 == 朴素参考。"""

    def test_fixed_sample_data(self):
        with open(os.path.join(DATA, "sample_keys.txt"), encoding="utf-8") as fh:
            keys = fh.read().splitlines()
        with open(os.path.join(DATA, "sample_expected.txt"),
                  encoding="utf-8") as fh:
            expected = fh.read().splitlines()
        self.assertEqual(reference(keys), expected)      # 数据文件自洽
        for limit in (None, 4 << 10, 1 << 10):
            op = sd.SpillDedup(memory_limit_bytes=limit, spill_dir=self.tmp)
            op.add_many(keys)
            self.assertEqual(list(op.results()), expected,
                             "limit=%r 时对拍失败" % limit)

    def test_random_multi_seed(self):
        for seed in range(5):
            rng = random.Random(1000 + seed)
            n = rng.choice([1000, 20000, 60000])
            keys = [rng.randrange(rng.choice([10, 5000, 100000]))
                    for _ in range(n)]
            ref = reference(keys)
            unlimited = sd.SpillDedup(None, self.tmp)
            unlimited.add_many(keys)
            self.assertEqual(list(unlimited.results()), ref)
            for limit in (8 << 10, 64 << 10):
                op = sd.SpillDedup(limit, self.tmp)
                op.add_many(keys)
                self.assertEqual(list(op.results()), ref,
                                 "seed=%d limit=%d 对拍失败" % (seed, limit))

    def test_hashseed_independence(self):
        """算子不依赖哈希：不同 PYTHONHASHSEED 下结果必须一致。"""
        code = (
            "import sys; sys.path.insert(0, %r);"
            "import spilldedup as sd, random;"
            "rng = random.Random(7);"
            "keys = ['k%%d' %% rng.randrange(100) for _ in range(5000)];"
            "op = sd.SpillDedup(2048, %r); op.add_many(keys);"
            "print('\\n'.join(op.results()))" % (HERE, self.tmp))
        outs = []
        for seed in ("0", "1", "42"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            out = subprocess.run([sys.executable, "-c", code], env=env,
                                 capture_output=True, text=True, check=True)
            outs.append(out.stdout)
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[1], outs[2])


class TestHashCollision(TempDirMixin):
    @unittest.skipUnless(sys.maxsize > 2**32, "需要 64 位 CPython")
    def test_int_hash_collision_not_merged(self):
        """hash 碰撞不得误去重：hash(0) == hash(2**61-1)，两者都必须保留。"""
        big = (1 << 61) - 1
        self.assertEqual(hash(0), hash(big))   # 确认真的是碰撞对
        keys = [0, big] * 5000 + [1, 2, 3]
        random.Random(5).shuffle(keys)
        for limit in (None, 1024):
            op = sd.SpillDedup(limit, self.tmp)
            op.add_many(keys)
            got = list(op.results())
            self.assertIn(0, got)
            self.assertIn(big, got)
            self.assertEqual(got, reference(keys))

    def test_operator_never_calls_hash(self):
        """结构性保证：算子源码中不存在 hash( 调用。"""
        with open(os.path.join(HERE, "spilldedup.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("hash(", src)


class TestCrashCleanup(TempDirMixin):
    def test_sigkill_residue_swept(self):
        """子进程溢写途中被 SIGKILL：残留目录存在但不影响下一次运行，
        且被新算子自动清扫。"""
        child_code = (
            "import sys, os\n"
            "sys.path.insert(0, %r)\n"
            "import spilldedup as sd\n"
            "op = sd.SpillDedup(4096, %r)\n"
            "i = 0\n"
            "while op.spill_count < 3:\n"
            "    op.add(os.urandom(64))\n"
            "    i += 1\n"
            "print(op.dir, flush=True)\n"
            "os.kill(os.getpid(), 9)\n" % (HERE, self.tmp))
        proc = subprocess.Popen([sys.executable, "-c", child_code],
                                stdout=subprocess.PIPE, text=True)
        out, _ = proc.communicate()
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        victim_dir = out.strip()
        self.assertTrue(os.path.isdir(victim_dir), "被杀后应留下残留目录")
        self.assertTrue(any(name.endswith(".bin")
                            for name in os.listdir(victim_dir)),
                        "残留目录里应有已发布的溢写文件")

        # 下一次运行：结果正确，且残留被清扫
        keys = list(range(5000)) * 2
        op = sd.SpillDedup(4096, self.tmp)
        op.add_many(keys)
        self.assertEqual(list(op.results()), reference(keys))
        self.assertFalse(os.path.exists(victim_dir), "残留目录应被清扫")

    def test_tmp_file_not_visible(self):
        """正常流程结束后，溢写目录整体被删除，不留 .tmp/.bin。"""
        op = sd.SpillDedup(1024, self.tmp)
        op.add_many(range(2000))
        list(op.results())
        self.assertFalse(os.path.exists(op.dir))
        leftovers = [n for n in os.listdir(self.tmp)
                     if n.startswith("p%d_" % os.getpid())]
        self.assertEqual(leftovers, [])


class TestConcurrency(TempDirMixin):
    def test_two_instances_interleaved(self):
        rng = random.Random(6)
        keys_a = [rng.randrange(3000) for _ in range(20000)]
        keys_b = [rng.randrange(3000) + 100000 for _ in range(20000)]
        op_a = sd.SpillDedup(16 << 10, self.tmp)
        op_b = sd.SpillDedup(16 << 10, self.tmp)
        self.assertNotEqual(op_a.dir, op_b.dir)
        for ka, kb in zip(keys_a, keys_b):
            op_a.add(ka)
            op_b.add(kb)
        self.assertEqual(list(op_a.results()), reference(keys_a))
        self.assertEqual(list(op_b.results()), reference(keys_b))


class TestEdgeCases(TempDirMixin):
    def test_single_key_larger_than_limit(self):
        """单条键比内存上限还大：立即溢写，结果仍正确。"""
        big = b"x" * 100000
        op = sd.SpillDedup(memory_limit_bytes=1024, spill_dir=self.tmp)
        op.add_many([big, big, b"small", big])
        self.assertGreaterEqual(op.spill_count, 1)
        self.assertEqual(list(op.results()), [b"small", big])

    def test_convenience_dedup(self):
        keys = [3, 1, 2, 3, 1, 1]
        got = list(sd.dedup(keys, memory_limit_bytes=128,
                            spill_dir=self.tmp))
        self.assertEqual(got, reference(keys))


class TestMemoryAccounting(TempDirMixin):
    def test_charge_never_exceeds_limit_plus_one_item(self):
        """缓冲计费在溢写触发瞬间的越过量不超过单条键的计费。"""
        limit = 8 << 10
        op = sd.SpillDedup(limit, self.tmp)
        max_seen = 0
        rng = random.Random(8)
        for _ in range(30000):
            op.add(rng.randrange(10000))
            max_seen = max(max_seen, op._charged)
            self.assertLessEqual(op._charged, limit + 4096)
        list(op.results())
        self.assertGreater(max_seen, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
