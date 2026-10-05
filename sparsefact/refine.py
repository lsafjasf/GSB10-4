"""Residual computation and iterative refinement."""


def residual_norm(mat, x, b):
    """||b - A x||_inf / ||b||_inf."""
    r = mat.matvec(x)
    num = 0.0
    den = 0.0
    for i in range(mat.n):
        d = abs(b[i] - r[i])
        if d > num:
            num = d
        bb = abs(b[i])
        if bb > den:
            den = bb
    return num / (den or 1.0)


def iterative_refinement(mat, factor, b, tol=1e-12, max_iter=20,
                         x_true=None):
    """Solve A x = b with the factor, then refine:
        r = b - A x;  x += A^{-1} r   (using the same factor)
    Returns (x, history) where history[k] = (residual, forward_error)
    after k refinement sweeps (history[0] = raw solve).  forward_error
    is None unless x_true is given.  Note: Cholesky is backward stable,
    so the residual is ~machine epsilon immediately; refinement improves
    the *forward* error of ill-conditioned systems.
    """
    def fwd_err(x):
        if x_true is None:
            return None
        num = max(abs(a - c) for a, c in zip(x, x_true))
        den = max(abs(c) for c in x_true) or 1.0
        return num / den

    x = factor.solve_system(b)
    history = [(residual_norm(mat, x, b), fwd_err(x))]
    it = 0
    while history[-1][0] > tol and it < max_iter:
        ax = mat.matvec(x)
        r = [b[i] - ax[i] for i in range(mat.n)]
        dx = factor.solve_system(r)
        x = [xi + di for xi, di in zip(x, dx)]
        history.append((residual_norm(mat, x, b), fwd_err(x)))
        it += 1
        if x_true is None and history[-1][0] <= tol:
            break
    return x, history
