"""测试/实验用图模型生成器（均为二元变量成对 MRF 或含确定性因子的小图）。"""

import math
import random

from .factor_graph import FactorGraph


def _pair_table(j00, j01, j10, j11):
    return [j00, j01, j10, j11]


def chain_mrf(n, rng, field_scale=0.5, coupling_scale=0.5):
    """链式（树）MRF：BP 在树上应达到精确。"""
    g = FactorGraph([2] * n)
    for v in range(n):
        h = rng.uniform(-field_scale, field_scale)
        g.add_factor([v], [math.exp(-h), math.exp(h)])
    for v in range(n - 1):
        j = rng.uniform(-coupling_scale, coupling_scale)
        g.add_factor(
            [v, v + 1],
            _pair_table(math.exp(j), math.exp(-j), math.exp(-j), math.exp(j)),
        )
    return g


def grid_mrf(rows, cols, rng, field_scale=0.4, coupling_scale=0.4):
    """二维网格（含环）MRF。"""
    n = rows * cols
    g = FactorGraph([2] * n)

    def vid(r, c):
        return r * cols + c

    for r in range(rows):
        for c in range(cols):
            h = rng.uniform(-field_scale, field_scale)
            g.add_factor([vid(r, c)], [math.exp(-h), math.exp(h)])
    for r in range(rows):
        for c in range(cols):
            if c + 1 < cols:
                j = rng.uniform(-coupling_scale, coupling_scale)
                g.add_factor(
                    [vid(r, c), vid(r, c + 1)],
                    _pair_table(
                        math.exp(j), math.exp(-j), math.exp(-j), math.exp(j)
                    ),
                )
            if r + 1 < rows:
                j = rng.uniform(-coupling_scale, coupling_scale)
                g.add_factor(
                    [vid(r, c), vid(r + 1, c)],
                    _pair_table(
                        math.exp(j), math.exp(-j), math.exp(-j), math.exp(j)
                    ),
                )
    return g


def ising_grid(rows, cols, beta, rng, field_scale=0.05, frustrated=True):
    """强耦合 Ising 网格：beta 大时无阻尼 BP 易振荡/不收敛。

    frustrated=True 时耦合符号随机（自旋玻璃），更容易诱发振荡。
    """
    n = rows * cols
    g = FactorGraph([2] * n)

    def vid(r, c):
        return r * cols + c

    for v in range(n):
        h = rng.uniform(-field_scale, field_scale)
        g.add_factor([v], [math.exp(-h), math.exp(h)])
    for r in range(rows):
        for c in range(cols):
            for dr, dc in ((0, 1), (1, 0)):
                r2, c2 = r + dr, c + dc
                if r2 < rows and c2 < cols:
                    j = beta
                    if frustrated and rng.random() < 0.5:
                        j = -beta
                    g.add_factor(
                        [vid(r, c), vid(r2, c2)],
                        _pair_table(
                            math.exp(j), math.exp(-j), math.exp(-j), math.exp(j)
                        ),
                    )
    return g


def random_graph_mrf(n, m, rng, field_scale=0.4, coupling_scale=0.4):
    """随机图 MRF：n 个变量、恰好 m 条成对边。"""
    g = FactorGraph([2] * n)
    for v in range(n):
        h = rng.uniform(-field_scale, field_scale)
        g.add_factor([v], [math.exp(-h), math.exp(h)])
    all_edges = (n * (n - 1)) // 2
    if m > all_edges:
        raise ValueError("边数超出完全图容量")
    edges = set()
    while len(edges) < m:
        a = rng.randrange(n)
        b = rng.randrange(n)
        if a == b:
            continue
        e = (a, b) if a < b else (b, a)
        edges.add(e)
    for a, b in edges:
        j = rng.uniform(-coupling_scale, coupling_scale)
        g.add_factor(
            [a, b],
            _pair_table(math.exp(j), math.exp(-j), math.exp(-j), math.exp(j)),
        )
    return g


def degenerate_zero_graph():
    """消息退化样例：确定性约束使部分消息出现零分量。

    结构：x0 == x1（硬相等），x1 XOR x2 == 1（硬异或），
    另加强一元势把 x0 拉向 0。确定性因子会产生全零方向的消息，
    检验防护逻辑不会输出 NaN/负概率。
    """
    g = FactorGraph([2, 2, 2])
    g.add_factor([0], [3.0, 1.0])
    g.add_factor([2], [1.0, 2.0])
    g.add_factor([0, 1], [1.0, 0.0, 0.0, 1.0])   # x0 == x1
    g.add_factor([1, 2], [0.0, 1.0, 1.0, 0.0])   # x1 XOR x2 == 1
    return g


def degenerate_contradiction_graph():
    """局部矛盾的硬约束：AND 型因子只允许 (x0,x1)=(0,0)，
    但一元势强烈要求 x0=1。

    强一元势把 m_{x0->f} 推成 [~0, 1] 后，因子发往 x1 的消息两个
    分量都约为 0（零和退化消息）。用于检验：全零消息回退均匀分布并
    告警，绝不输出 NaN/负概率；同时结果被明确标记为不可直接采信。
    """
    g = FactorGraph([2, 2])
    g.add_factor([0], [0.0, 1.0])                    # 硬性要求 x0=1
    g.add_factor([0, 1], [1.0, 0.0, 0.0, 0.0])       # AND：仅 (0,0) 允许
    return g
