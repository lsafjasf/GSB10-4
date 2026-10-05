"""迭代精化：用 LU 分解反复修正解，记录每步残差（残差-迭代次数数据）。"""


def relative_residual(matrix, x, b):
    r = [bi - si for bi, si in zip(b, matrix.matvec(x))]
    nr = max(abs(v) for v in r)
    nb = max(abs(v) for v in b) or 1.0
    return nr / nb


def iterative_refinement(matrix, lu, b, max_iter=10, tol=1e-10):
    """返回 (x, history)；history = [(迭代次数, 相对残差 inf-范数), ...]。

    迭代 0 对应初始 LU 解；之后每轮 r=b-Ax，x <- x + U^{-1} L^{-1} r。
    """
    x = lu.solve(b)
    history = []
    for it in range(max_iter + 1):
        res = relative_residual(matrix, x, b)
        history.append((it, res))
        if res <= tol:
            break
        r = [bi - si for bi, si in zip(b, matrix.matvec(x))]
        dx = lu.solve(r)
        x = [xi + d for xi, d in zip(x, dx)]
    return x, history
