"""符号分解：只做布尔模式上的消去，统计 L/U 非零数与填充量。

填充规则（高斯消去，无选主元）：
  第 k 步更新  a_ij <- a_ij - (a_ik / a_kk) * a_kj
  若 a_ik != 0 且 a_kj != 0 而原 a_ij == 0，则 (i, j) 产生一个填充元。
图论表述：消去顶点 k 时，其尚未消去的邻居两两连边（形成一个团）。
L 的填充来自列方向 (i>k, a_ik!=0)，U 的填充来自行方向 (j>k, a_kj!=0)。
"""

from .memory import MemoryGuard


class SymbolicResult:
    __slots__ = ("l_nnz", "u_nnz", "a_nnz", "fill", "peak_live_entries")

    def __init__(self, l_nnz, u_nnz, a_nnz, peak_live_entries):
        self.l_nnz = l_nnz
        self.u_nnz = u_nnz
        self.a_nnz = a_nnz
        self.fill = l_nnz + u_nnz - a_nnz
        self.peak_live_entries = peak_live_entries

    @property
    def lu_nnz(self):
        return self.l_nnz + self.u_nnz

    def __repr__(self):
        return (f"SymbolicResult(A nnz={self.a_nnz}, L nnz={self.l_nnz}, "
                f"U nnz={self.u_nnz}, LU nnz={self.lu_nnz}, fill={self.fill})")


def symbolic_lu(matrix, guard=None, check_interval=1024):
    """返回 SymbolicResult。guard 为 MemoryGuard 时同样受内存预算约束。"""
    n = matrix.n
    rows = [set(r.keys()) for r in matrix.rows]
    colrows = [set() for _ in range(n)]  # colrows[j] = {i > j : j in rows[i]}
    for i in range(n):
        for j in rows[i]:
            if i > j:
                colrows[j].add(i)

    l_nnz = 0
    u_nnz = 0
    live = sum(len(r) for r in rows)
    peak_live = live
    for k in range(n):
        uk = {j for j in rows[k] if j >= k}
        u_nnz += len(uk)
        live -= len(rows[k])
        rows[k] = set()
        below = colrows[k]
        colrows[k] = set()
        live -= len(below)  # 每行删去列 k
        for i in below:
            ri = rows[i]
            ri.discard(k)
            l_nnz += 1
            for j in uk:
                if j > k and j not in ri:
                    ri.add(j)
                    live += 1
                    if i > j:
                        colrows[j].add(i)
        if live > peak_live:
            peak_live = live
        if guard is not None and k % check_interval == 0:
            guard.check(live + l_nnz + u_nnz, context=f"符号分解 pivot {k}")
    if guard is not None:
        guard.check(live + l_nnz + u_nnz, context="符号分解完成")
    return SymbolicResult(l_nnz, u_nnz, matrix.nnz, peak_live)
