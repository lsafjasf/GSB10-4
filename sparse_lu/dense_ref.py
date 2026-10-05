"""稠密参考 LU（仅小规模自测用，无选主元，Doolittle 形式）。"""


def dense_lu(matrix):
    a = matrix.to_dense()
    n = matrix.n
    for k in range(n):
        pivot = a[k][k]
        for i in range(k + 1, n):
            a[i][k] /= pivot
            aik = a[i][k]
            for j in range(k + 1, n):
                a[i][j] -= aik * a[k][j]
    l = [[0.0] * n for _ in range(n)]
    u = [[0.0] * n for _ in range(n)]
    for i in range(n):
        l[i][i] = 1.0
        for j in range(n):
            if i > j:
                l[i][j] = a[i][j]
            else:
                u[i][j] = a[i][j]
    return l, u


def dense_residual(matrix, l, u):
    n = matrix.n
    err = 0.0
    scale = 0.0
    for i in range(n):
        for j in range(n):
            s = 0.0
            for k in range(n):
                s += l[i][k] * u[k][j]
            aij = matrix.rows[i].get(j, 0.0)
            err = max(err, abs(aij - s))
            scale = max(scale, abs(aij))
    return err / (scale or 1.0)
