"""随机化算法的失败概率控制：单侧 Monte Carlo 的重复放大。

具体载体：Freivalds 矩阵乘积验证（在 GF(2) 上）。

给定 A,B,C ∈ GF(2)^(n×n)，判断 AB == C：
  1. 随机抽取 r ∈ {0,1}^n（每个分量独立均匀，r ≠ 0 与否均可，此处含 0）；
  2. 计算 Br、A(Br)、Cr；
  3. 若 A(Br) == Cr 输出 ACCEPT，否则输出 REJECT。

关键性质（单侧错误，只会把“错”误判成“对”）：
  * AB == C           ⇒ 永远 ACCEPT（假阳性概率 0，确定性输入不失效）；
  * AB != C，令 D=AB⊕C≠0 ⇒ P(ACCEPT) = P(Dr=0) ≤ 1/2；
    证明：D 必有非零行 d（含 j 使 D_ij=1），固定 r 除 r_j 外的分量，
    等式 d·r=0 对 r_j 是一个非平凡线性方程，两个取值恰有一个满足，故概率为 1/2。
    该上界对所有非零 D 一致，且可以取等（见 make_rank1_witness）。

重复 k 次独立试验（随机向量相互独立），全部 ACCEPT 才 ACCEPT：
  P(失败) ≤ (1/2)^k。
要使 P(失败) ≤ ε，只需 k = ceil(-log2 ε)，即 trials_needed(eps)。

本模块同时给出两份实现，便于对拍：
  * verify_naive：矩阵用 list[list[int]]，O(n^2) 每次试验（n 维点积）；
  * verify_packed：矩阵行打包进 Python int（GF(2) 下乘法为 &、加法为 ^），
                   同样的算法，只是位级实现。

随机源通过 RandomSource 协议注入；SeededSource 仅基于标准库 random.Random
（Mersenne Twister），同种子产生完全相同的比特流，结论可复现。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import List, Protocol, Sequence, Tuple


class DegenerateRandomnessError(ValueError):
    """随机源退化（常量比特流或恒定试验向量），重复试验不再独立时抛出。"""


class RandomSource(Protocol):
    """可注入的随机源协议：只需能产生一个独立均匀比特。"""

    def random_bit(self) -> int:
        ...


class SeededSource:
    """基于标准库 random.Random 的可复现随机源。

    同一种子产生完全相同的比特序列（random.Random.getrandbits 是确定性的）。
    """

    def __init__(self, seed: int = 0):
        self._rng = random.Random(seed)
        self.seed = seed

    def random_bit(self) -> int:
        return self._rng.getrandbits(1)


class ConstantBitSource:
    """退化源 A：每次都返回同一个比特。"""

    def __init__(self, value: int):
        self.value = 1 if value else 0

    def random_bit(self) -> int:
        return self.value


class FixedVectorSource:
    """退化源 B：每次试验产生同一个固定向量（分量仍可 0/1 混合）。

    重复 k 次得到的是同一个判定，独立重复的放大保证完全失效。
    实现为周期 n 的循环比特流：random_bit 依次吐出固定向量的各分量。
    """

    def __init__(self, vector: Sequence[int]):
        self._v = [1 if x else 0 for x in vector]
        self._pos = 0

    def random_bit(self) -> int:
        b = self._v[self._pos]
        self._pos = (self._pos + 1) % len(self._v)
        return b

    def random_vector(self, n: int) -> List[int]:
        if n != len(self._v):
            raise ValueError("固定向量长度与 n 不一致")
        return list(self._v)


def health_check(source: RandomSource, n: int, probes: int = 64) -> None:
    """随机源健康检查：构造性地识别两类退化，不依赖“失败真的发生”。

    1) 比特流退化：连续 `probes` 个比特全相同（周期 1）→ ConstantBitSource；
    2) 试验向量退化：比特流存在周期 n（s[i]≡s[i+n]），于是连续取出的
       n 维试验向量完全相同 → FixedVectorSource 一类（要求 n ≤ probes/2）。

    对健康的独立均匀比特流，误报概率 ≤ 2^-(probes-1) + 2^-(probes-n)。
    """

    if probes < 32:
        raise ValueError("probes 过小")
    bits = [source.random_bit() for _ in range(probes)]
    if all(b == bits[0] for b in bits):
        raise DegenerateRandomnessError(
            "随机源退化：连续 %d 个比特全部为 %d，重复试验不独立"
            % (probes, bits[0])
        )
    if 2 <= n <= probes // 2:
        if all(bits[i] == bits[i + n] for i in range(probes - n)):
            raise DegenerateRandomnessError(
                "随机源退化：比特流存在周期 %d，连续试验向量完全相同，"
                "独立重复放大失效" % n
            )


def trials_needed(epsilon: float) -> int:
    """返回使 (1/2)^k ≤ epsilon 的最小整数 k。"""

    if not (0.0 < epsilon < 1.0):
        raise ValueError("epsilon 必须位于 (0, 1)")
    return math.ceil(-math.log2(epsilon))


# --------------------------------------------------------------------------- #
# 朴素实现：list[list[int]]，GF(2)
# --------------------------------------------------------------------------- #

Matrix = List[List[int]]


def gf2_matmul(a: Matrix, b: Matrix) -> Matrix:
    """GF(2) 矩阵乘法（朴素，用于构造测试输入与对拍）。"""

    n = len(a)
    bt = [list(col) for col in zip(*b)]
    out = [[0] * n for _ in range(n)]
    for i in range(n):
        ai = a[i]
        for j in range(n):
            s = 0
            bj = bt[j]
            for k in range(n):
                s ^= ai[k] & bj[k]
            out[i][j] = s
    return out


def gf2_matvec(a: Matrix, x: List[int]) -> List[int]:
    return [sum((a[i][j] & x[j] for j in range(len(x))), 0) & 1 for i in range(len(a))]


def _random_vector(source: RandomSource, n: int) -> List[int]:
    return [source.random_bit() for _ in range(n)]


def freivalds_once_naive(
    a: Matrix, b: Matrix, c: Matrix, source: RandomSource
) -> bool:
    """单次 Freivalds 试验，返回 True=ACCEPT。"""

    n = len(a)
    r = _random_vector(source, n)
    br = gf2_matvec(b, r)
    left = gf2_matvec(a, br)
    right = gf2_matvec(c, r)
    return left == right


def verify_naive(
    a: Matrix,
    b: Matrix,
    c: Matrix,
    source: RandomSource,
    k: int,
    *,
    check: bool = True,
) -> bool:
    """重复 k 次独立试验的朴素实现；任一 REJECT 立即 REJECT（短路）。"""

    if check:
        health_check(source, len(a))
    for _ in range(k):
        if not freivalds_once_naive(a, b, c, source):
            return False
    return True


# --------------------------------------------------------------------------- #
# 位打包实现：每行一个 Python int（位 j 表示第 j 列），GF(2) 下 & = 乘，^ = 加
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BitMatrix:
    n: int
    rows: Tuple[int, ...]

    @classmethod
    def from_rows(cls, m: Sequence[Sequence[int]]) -> "BitMatrix":
        n = len(m)
        packed = []
        for row in m:
            v = 0
            for j, x in enumerate(row):
                v |= (x & 1) << j
            packed.append(v)
        return cls(n, tuple(packed))

    def matvec(self, r: int) -> int:
        """(this·r) mod 2，r 为打包向量；返回打包向量。

        结果位 i = parity(rows[i] & r)；CPython 对 int.bit_count 有原生加速。
        """

        out = 0
        for i, row in enumerate(self.rows):
            out |= ((row & r).bit_count() & 1) << i
        return out

    def matmul(self, other: "BitMatrix") -> "BitMatrix":
        n = self.n
        cols = [0] * n
        for i, row in enumerate(other.rows):
            x = row
            j = 0
            while x:
                if x & 1:
                    cols[j] |= 1 << i
                x >>= 1
                j += 1
        out_rows = []
        for row in self.rows:
            v = 0
            for j, col in enumerate(cols):
                v |= ((row & col).bit_count() & 1) << j
            out_rows.append(v)
        return BitMatrix(n, tuple(out_rows))


def _random_packed_vector(source: RandomSource, n: int) -> int:
    r = 0
    for j in range(n):
        r |= source.random_bit() << j
    return r


def freivalds_once_packed(
    a: BitMatrix, b: BitMatrix, c: BitMatrix, source: RandomSource
) -> bool:
    r = _random_packed_vector(source, a.n)
    return a.matvec(b.matvec(r)) == c.matvec(r)


def verify_packed(
    a: BitMatrix,
    b: BitMatrix,
    c: BitMatrix,
    source: RandomSource,
    k: int,
    *,
    check: bool = True,
) -> bool:
    """重复 k 次独立试验的位打包实现。"""

    if check:
        health_check(source, a.n)
    for _ in range(k):
        if not freivalds_once_packed(a, b, c, source):
            return False
    return True
