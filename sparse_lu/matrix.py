"""稀疏矩阵结构（行字典 / DOK）与测试矩阵生成器。仅用标准库。"""

import math
import random


class SparseMatrix:
    """n x n 稀疏矩阵，rows[i] = {列号 j: 数值 a_ij}。"""

    __slots__ = ("n", "rows")

    def __init__(self, n, rows=None):
        self.n = n
        self.rows = rows if rows is not None else [dict() for _ in range(n)]

    @property
    def nnz(self):
        return sum(len(r) for r in self.rows)

    def copy(self):
        return SparseMatrix(self.n, [dict(r) for r in self.rows])

    def matvec(self, x):
        y = [0.0] * self.n
        for i, ri in enumerate(self.rows):
            s = 0.0
            for j, v in ri.items():
                s += v * x[j]
            y[i] = s
        return y

    def frobenius_norm(self):
        return math.sqrt(sum(v * v for r in self.rows for v in r.values()))

    def max_abs(self):
        return max((abs(v) for r in self.rows for v in r.values()), default=0.0)

    def symmetrized_pattern(self):
        """返回 A + A^T 的邻接集合（去掉自环），供重排算法使用。"""
        adj = [set() for _ in range(self.n)]
        for i, ri in enumerate(self.rows):
            for j in ri:
                if j != i:
                    adj[i].add(j)
                    adj[j].add(i)
        return adj

    def permute_symmetric(self, perm):
        """对称置换 B = P A P^T；perm[新编号] = 旧编号。

        不做选主元时必须保持对角元在对角线上，因此填充重排使用对称置换。
        """
        n = self.n
        inv = [0] * n
        for new, old in enumerate(perm):
            inv[old] = new
        rows = [dict() for _ in range(n)]
        for new_i, old_i in enumerate(perm):
            dst = rows[new_i]
            for old_j, v in self.rows[old_i].items():
                dst[inv[old_j]] = v
        return SparseMatrix(n, rows)

    def to_dense(self):
        n = self.n
        a = [[0.0] * n for _ in range(n)]
        for i, ri in enumerate(self.rows):
            for j, v in ri.items():
                a[i][j] = v
        return a


def _diag_dominant(rows, n, slack=1.0):
    """把对角元设为行内绝对值之和 + slack，保证严格对角占优（无需选主元）。"""
    for i in range(n):
        s = sum(abs(v) for j, v in rows[i].items() if j != i)
        rows[i][i] = s + slack


def diagonal_matrix(n, slack=1.0):
    rows = [dict() for _ in range(n)]
    for i in range(n):
        rows[i][i] = float(i + 1) + slack
    return SparseMatrix(n, rows)


def banded_matrix(n, bw, seed=0, slack=1.0):
    """带状矩阵：|i-j|<=bw 内稠密、对角占优。半带宽 bw。"""
    rng = random.Random(seed)
    rows = [dict() for _ in range(n)]
    for i in range(n):
        ri = rows[i]
        for j in range(max(0, i - bw), min(n, i + bw + 1)):
            if j != i:
                ri[j] = rng.uniform(-1.0, 1.0)
    _diag_dominant(rows, n, slack)
    return SparseMatrix(n, rows)


def random_sparse_matrix(n, nnz_target, seed=0, slack=1.0):
    """随机稀疏矩阵：均匀采样 nnz_target 个非零位置（允许重复采样，按位置去重）。

    最终非零数约为 nnz_target；对角占优。
    """
    rng = random.Random(seed)
    rows = [dict() for _ in range(n)]
    placed = 0
    while placed < nnz_target:
        i = rng.randrange(n)
        j = rng.randrange(n)
        if i == j:
            continue
        if j not in rows[i]:
            rows[i][j] = rng.uniform(-1.0, 1.0)
            placed += 1
    _diag_dominant(rows, n, slack)
    return SparseMatrix(n, rows)


def near_dense_matrix(n, density=0.9, seed=0, slack=1.0):
    """接近稠密的矩阵：每个非对角位置以概率 density 存在。"""
    rng = random.Random(seed)
    rows = [dict() for _ in range(n)]
    for i in range(n):
        ri = rows[i]
        for j in range(n):
            if j != i and rng.random() < density:
                ri[j] = rng.uniform(-1.0, 1.0)
    _diag_dominant(rows, n, slack)
    return SparseMatrix(n, rows)


def poisson_2d_matrix(nx, ny, nine_point=True, seed=0):
    """2D 网格离散矩阵（五点或九点模板），严格对角占优。"""
    rng = random.Random(seed)
    n = nx * ny

    def node(x, y):
        return y * nx + x

    rows = [dict() for _ in range(n)]
    offsets5 = ((1, 0), (-1, 0), (0, 1), (0, -1))
    offsets9 = offsets5 + ((1, 1), (1, -1), (-1, 1), (-1, -1))
    offsets = offsets9 if nine_point else offsets5
    weight = -1.0 if not nine_point else -0.25
    for y in range(ny):
        for x in range(nx):
            i = node(x, y)
            ri = rows[i]
            for dx, dy in offsets:
                xx, yy = x + dx, y + dy
                if 0 <= xx < nx and 0 <= yy < ny:
                    j = node(xx, yy)
                    ri[j] = weight * (0.5 + rng.random())
    for i in range(n):
        rows[i][i] = 4.0 if not nine_point else 8.0
    return SparseMatrix(n, rows)


def ill_conditioned_banded(n, bw, slack=1e-6, seed=0):
    """近奇异带状矩阵：非对角元全部同号，diag=行内绝对值之和+slack。

    非对角元取负（M-矩阵结构），行和恒为 slack，常数向量对应特征值 ≈ slack，
    条件数 ~ O(1/slack)。仍严格对角占优，无选主元 LU 稳定但初始解残差较大，
    适合展示迭代精化。
    """
    rng = random.Random(seed)
    rows = [dict() for _ in range(n)]
    for i in range(n):
        ri = rows[i]
        s = 0.0
        for j in range(max(0, i - bw), min(n, i + bw + 1)):
            if j != i:
                v = -(0.5 + 0.5 * rng.random())
                ri[j] = v
                s -= v
        ri[i] = s + slack
    return SparseMatrix(n, rows)
