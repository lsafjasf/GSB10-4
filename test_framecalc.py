"""framecalc 自测（仅标准库 unittest）。

三层对拍：
1. 手工推算的逐行期望表（字面量断言）；
2. 独立暴力实现 brute_force_frames 与优化实现逐帧比对（随机数据 + 并列值）；
3. 重复运行 / 打乱输入顺序的一致性。
"""

import random
import unittest

from framecalc import (
    Bound, BoundType, FrameMode, FrameSpec,
    compute_frames as _compute_frames, sort_order, window_aggregate,
)


def compute_frames(spec, sorted_keys):
    """把库的 (los, his) 并行数组转成测试友好的 [(lo, hi), ...]。"""
    los, his = _compute_frames(spec, sorted_keys)
    return list(zip(los, his))

UBP = Bound(BoundType.UNBOUNDED_PRECEDING)
UBF = Bound(BoundType.UNBOUNDED_FOLLOWING)
CUR = Bound(BoundType.CURRENT_ROW)


def P(n):
    return Bound(BoundType.PRECEDING, n)


def F(n):
    return Bound(BoundType.FOLLOWING, n)


# ---------------------------------------------------------------- 暴力参考实现

def brute_force_frames(spec, sorted_keys):
    """对每一行 i，逐行扫描所有 j 判断是否在帧内。与优化实现完全独立。"""
    n = len(sorted_keys)
    frames = []
    for i in range(n):
        lo, hi = None, None
        for j in range(n):
            if _in_frame(spec, sorted_keys, i, j, n):
                if lo is None:
                    lo = j
                hi = j
        frames.append((0, -1) if lo is None else (lo, hi))
    return frames


def _in_frame(spec, keys, i, j, n):
    def bound_pos(b, is_start):
        t = b.type
        if spec.mode is FrameMode.ROWS:
            if t is BoundType.UNBOUNDED_PRECEDING:
                return 0
            if t is BoundType.UNBOUNDED_FOLLOWING:
                return n - 1
            if t is BoundType.CURRENT_ROW:
                return i
            raw = i - b.offset if t is BoundType.PRECEDING else i + b.offset
            return max(0, raw) if is_start else min(n - 1, raw)
        if spec.mode is FrameMode.RANGE:
            if t is BoundType.UNBOUNDED_PRECEDING:
                return None
            if t is BoundType.UNBOUNDED_FOLLOWING:
                return None
            if t is BoundType.CURRENT_ROW:
                return keys[i]
            return keys[i] - b.offset if t is BoundType.PRECEDING else keys[i] + b.offset
        # GROUPS：先求行 i / 行 j 所在的并列组编号
        raise AssertionError("GROUPS 走 _in_frame_groups")

    if spec.mode is FrameMode.GROUPS:
        return _in_frame_groups(spec, keys, i, j, n)

    if spec.mode is FrameMode.ROWS:
        lo = bound_pos(spec.start, True)
        hi = bound_pos(spec.end, False)
        return lo <= j <= hi
    # RANGE：按值判断
    s, e = spec.start, spec.end
    lo_ok = True
    hi_ok = True
    if s.type is BoundType.PRECEDING:
        lo_ok = keys[j] >= keys[i] - s.offset
    elif s.type is BoundType.CURRENT_ROW:
        lo_ok = keys[j] >= keys[i]
    elif s.type is BoundType.FOLLOWING:
        lo_ok = keys[j] >= keys[i] + s.offset
    if e.type is BoundType.PRECEDING:
        hi_ok = keys[j] <= keys[i] - e.offset
    elif e.type is BoundType.CURRENT_ROW:
        hi_ok = keys[j] <= keys[i]
    elif e.type is BoundType.FOLLOWING:
        hi_ok = keys[j] <= keys[i] + e.offset
    return lo_ok and hi_ok


def _in_frame_groups(spec, keys, i, j, n):
    group_of = [0] * n
    g = -1
    for r in range(n):
        if r == 0 or keys[r] != keys[r - 1]:
            g += 1
        group_of[r] = g
    gi, gj = group_of[i], group_of[j]
    s, e = spec.start, spec.end
    lo_ok = hi_ok = True
    if s.type is BoundType.PRECEDING:
        lo_ok = gj >= gi - s.offset
    elif s.type is BoundType.CURRENT_ROW:
        lo_ok = gj >= gi
    elif s.type is BoundType.FOLLOWING:
        lo_ok = gj >= gi + s.offset
    if e.type is BoundType.PRECEDING:
        hi_ok = gj <= gi - e.offset
    elif e.type is BoundType.CURRENT_ROW:
        hi_ok = gj <= gi
    elif e.type is BoundType.FOLLOWING:
        hi_ok = gj <= gi + e.offset
    return lo_ok and hi_ok


# ---------------------------------------------------------------- 手工对拍用例

class HandComputedTest(unittest.TestCase):
    """逐行手工推算，期望值为字面量。"""

    def test_rows_1_preceding_1_following(self):
        # keys 已排序，values = 行号*10；帧 = [i-1, i+1] 闭区间，越界截断
        keys = [1, 2, 3, 4, 5]
        vals = [10, 20, 30, 40, 50]
        spec = FrameSpec(FrameMode.ROWS, P(1), F(1))
        frames = compute_frames(spec, keys)
        self.assertEqual(frames, [(0, 1), (0, 2), (1, 3), (2, 4), (3, 4)])
        self.assertEqual(
            window_aggregate(spec, keys, vals, "sum"),
            [30, 60, 90, 120, 90],
        )
        self.assertEqual(
            window_aggregate(spec, keys, vals, "count"), [2, 3, 3, 3, 2]
        )

    def test_rows_unbounded_preceding_to_current(self):
        keys = [1, 2, 3]
        vals = [1, 2, 3]
        spec = FrameSpec(FrameMode.ROWS, UBP, CUR)
        self.assertEqual(compute_frames(spec, keys), [(0, 0), (0, 1), (0, 2)])
        self.assertEqual(window_aggregate(spec, keys, vals, "sum"), [1, 3, 6])

    def test_full_frame(self):
        # 全帧：UNBOUNDED PRECEDING .. UNBOUNDED FOLLOWING，每行都是整个分区
        keys = [3, 1, 2]  # 未排序输入，内部排序后为 1,2,3
        vals = [30, 10, 20]
        spec = FrameSpec(FrameMode.ROWS, UBP, UBF)
        self.assertEqual(window_aggregate(spec, keys, vals, "sum"), [60, 60, 60])
        self.assertEqual(window_aggregate(spec, keys, vals, "count"), [3, 3, 3])
        self.assertEqual(window_aggregate(spec, keys, vals, "min"), [10, 10, 10])
        self.assertEqual(window_aggregate(spec, keys, vals, "max"), [30, 30, 30])

    def test_single_row(self):
        for mode in (FrameMode.ROWS, FrameMode.RANGE, FrameMode.GROUPS):
            spec = FrameSpec(mode, UBP, UBF)
            self.assertEqual(compute_frames(spec, [42]), [(0, 0)])
            self.assertEqual(window_aggregate(spec, [42], [7], "sum"), [7])
        spec = FrameSpec(FrameMode.ROWS, P(3), F(3))
        self.assertEqual(compute_frames(spec, [42]), [(0, 0)])

    def test_range_ties_merged(self):
        # 并列值并入：RANGE BETWEEN 1 PRECEDING AND CURRENT ROW
        # keys = [1, 2, 2, 2, 4]
        # 行0(k=1): 值域[0,1] -> 行0
        # 行1(k=2): 值域[1,2] -> 行0..3（三个并列的 2 全部并入）
        # 行2(k=2): 同上
        # 行3(k=2): 同上
        # 行4(k=4): 值域[3,4] -> 行4（2 不在 [3,4] 内，整体排除）
        keys = [1, 2, 2, 2, 4]
        vals = [1, 10, 20, 30, 5]
        spec = FrameSpec(FrameMode.RANGE, P(1), CUR)
        self.assertEqual(
            compute_frames(spec, keys),
            [(0, 0), (0, 3), (0, 3), (0, 3), (4, 4)],
        )
        self.assertEqual(
            window_aggregate(spec, keys, vals, "sum"), [1, 61, 61, 61, 5]
        )

    def test_range_current_row_includes_all_peers(self):
        keys = [5, 5, 5]
        vals = [1, 2, 3]
        spec = FrameSpec(FrameMode.RANGE, CUR, CUR)
        self.assertEqual(compute_frames(spec, keys), [(0, 2)] * 3)
        self.assertEqual(window_aggregate(spec, keys, vals, "sum"), [6, 6, 6])

    def test_rows_cuts_peer_group(self):
        # 对照：同一数据用 ROWS 1 PRECEDING，并列组被截断（物理行语义）
        keys = [1, 2, 2, 2, 4]
        vals = [1, 10, 20, 30, 5]
        spec = FrameSpec(FrameMode.ROWS, P(1), CUR)
        self.assertEqual(
            compute_frames(spec, keys),
            [(0, 0), (0, 1), (1, 2), (2, 3), (3, 4)],
        )
        self.assertEqual(
            window_aggregate(spec, keys, vals, "sum"), [1, 11, 30, 50, 35]
        )

    def test_groups_mode(self):
        # keys = [1, 2, 2, 2, 4] -> 3 个并列组: G0={0}, G1={1,2,3}, G2={4}
        keys = [1, 2, 2, 2, 4]
        vals = [1, 10, 20, 30, 5]
        spec = FrameSpec(FrameMode.GROUPS, P(1), CUR)
        self.assertEqual(
            compute_frames(spec, keys),
            [(0, 0), (0, 3), (0, 3), (0, 3), (1, 4)],
        )
        self.assertEqual(
            window_aggregate(spec, keys, vals, "sum"), [1, 61, 61, 61, 65]
        )

    def test_empty_frame_rows(self):
        # ROWS BETWEEN 3 FOLLOWING AND 5 FOLLOWING，4 行分区全部越界 -> 空帧
        keys = [1, 2, 3, 4]
        vals = [1, 2, 3, 4]
        spec = FrameSpec(FrameMode.ROWS, F(3), F(5))
        frames = compute_frames(spec, keys)
        # 起点只向下截断、终点只向上截断：
        # i=0 -> (3,3) 非空；i=1..3 -> lo>hi 空帧
        self.assertEqual(frames, [(3, 3), (4, 3), (5, 3), (6, 3)])
        sums = window_aggregate(spec, keys, vals, "sum")
        counts = window_aggregate(spec, keys, vals, "count")
        self.assertEqual(sums, [4, None, None, None])
        self.assertEqual(counts, [1, 0, 0, 0])
        self.assertEqual(window_aggregate(spec, keys, vals, "min"), [4, None, None, None])

    def test_empty_frame_range_gap(self):
        # RANGE BETWEEN 1 PRECEDING AND 1 PRECEDING：键值空洞处为空帧
        keys = [1, 10]
        vals = [100, 200]
        spec = FrameSpec(FrameMode.RANGE, P(1), P(1))
        self.assertEqual(compute_frames(spec, keys), [(0, -1), (1, 0)])
        self.assertEqual(window_aggregate(spec, keys, vals, "sum"), [None, None])
        self.assertEqual(window_aggregate(spec, keys, vals, "count"), [0, 0])

    def test_invalid_frame_rejected(self):
        with self.assertRaises(ValueError):
            FrameSpec(FrameMode.ROWS, CUR, P(1))
        with self.assertRaises(ValueError):
            Bound(BoundType.PRECEDING, -1)
        with self.assertRaises(ValueError):
            FrameSpec(FrameMode.ROWS, Bound(BoundType.UNBOUNDED_FOLLOWING),
                      Bound(BoundType.UNBOUNDED_FOLLOWING))
        with self.assertRaises(ValueError):
            FrameSpec(FrameMode.ROWS, Bound(BoundType.UNBOUNDED_PRECEDING),
                      Bound(BoundType.UNBOUNDED_PRECEDING))

    def test_same_category_reversed_offsets_is_always_empty(self):
        # 2 FOLLOWING .. 1 FOLLOWING：同类边界不按偏移比较（与 PostgreSQL 一致），
        # 接受该规格，但每帧恒为空。
        keys = [1, 2, 3, 4, 5]
        vals = [1, 2, 3, 4, 5]
        spec = FrameSpec(FrameMode.ROWS, F(2), F(1))
        self.assertEqual(
            compute_frames(spec, keys),
            [(2, 1), (3, 2), (4, 3), (5, 4), (6, 4)],
        )
        self.assertEqual(window_aggregate(spec, keys, vals, "sum"), [None] * 5)
        self.assertEqual(window_aggregate(spec, keys, vals, "count"), [0] * 5)


# ---------------------------------------------------------------- 暴力对拍

class DifferentialTest(unittest.TestCase):
    def _random_spec(self, rng, mode):
        def rand_bound(lo_rank, hi_rank):
            rank = rng.randint(lo_rank, hi_rank)
            if rank == 0:
                return Bound(BoundType.UNBOUNDED_PRECEDING)
            if rank == 1:
                return P(rng.randint(0, 4))
            if rank == 2:
                return CUR
            if rank == 3:
                return F(rng.randint(0, 4))
            return Bound(BoundType.UNBOUNDED_FOLLOWING)

        s = rand_bound(0, 3)
        e = rand_bound(max(s.type.value, 1), 4)
        return FrameSpec(mode, s, e)

    def test_optimized_matches_brute_force(self):
        rng = random.Random(20261006)
        for trial in range(300):
            n = rng.randint(1, 12)
            # 键取自小区间，制造大量并列
            keys = sorted(rng.randint(0, 5) for _ in range(n))
            mode = rng.choice(list(FrameMode))
            spec = self._random_spec(rng, mode)
            # 空帧只比较“为空”这一事实，不比较具体越界坐标
            norm = lambda fs: [(0, -1) if lo > hi else (lo, hi) for lo, hi in fs]
            got = norm(compute_frames(spec, keys))
            want = norm(brute_force_frames(spec, keys))
            self.assertEqual(
                got, want,
                "trial=%d mode=%s spec=%s keys=%s" % (trial, mode, spec, keys),
            )

    def test_sum_matches_direct_sum(self):
        rng = random.Random(7)
        for _ in range(200):
            n = rng.randint(1, 10)
            keys = [rng.randint(0, 4) for _ in range(n)]
            vals = [rng.randint(-50, 50) for _ in range(n)]
            mode = rng.choice(list(FrameMode))
            spec = self._random_spec(rng, mode)
            got = window_aggregate(spec, keys, vals, "sum")
            order = sort_order(keys)
            skeys = [keys[i] for i in order]
            svals = [vals[i] for i in order]
            frames = compute_frames(spec, skeys)
            want_sorted = [
                None if lo > hi else sum(svals[lo:hi + 1]) for lo, hi in frames
            ]
            want = [None] * n
            for pos, idx in enumerate(order):
                want[idx] = want_sorted[pos]
            self.assertEqual(got, want)


# ---------------------------------------------------------------- 可复现性

class DeterminismTest(unittest.TestCase):
    def test_repeated_runs_identical(self):
        rng = random.Random(99)
        keys = [rng.randint(0, 3) for _ in range(2000)]  # 大量重复键
        vals = list(range(2000))
        spec = FrameSpec(FrameMode.ROWS, P(2), F(2))
        first = window_aggregate(spec, keys, vals, "sum")
        for _ in range(5):
            self.assertEqual(window_aggregate(spec, keys, vals, "sum"), first)

    def test_consistent_across_processes_and_hash_seeds(self):
        # 同一输入在不同进程、不同 PYTHONHASHSEED 下结果必须逐字节一致。
        import hashlib
        import os
        import subprocess
        import sys

        script = (
            "import random, hashlib; "
            "from framecalc import *; "
            "rng = random.Random(42); "
            "keys = [rng.randint(0, 3) for _ in range(500)]; "
            "vals = list(range(500)); "
            "spec = FrameSpec(FrameMode.ROWS, "
            "Bound(BoundType.PRECEDING, 2), Bound(BoundType.FOLLOWING, 2)); "
            "r = window_aggregate(spec, keys, vals, 'sum'); "
            "print(hashlib.sha256(repr(r).encode()).hexdigest())"
        )
        digests = set()
        for seed in ("0", "1", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            out = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, env=env,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                check=True,
            )
            digests.add(out.stdout.strip())
        self.assertEqual(len(digests), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
