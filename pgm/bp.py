"""Loopy belief propagation (sum-product) on factor graphs.

Convergence protocol
--------------------
* residual(t)   = max over ALL messages (both directions) of the L-infinity
                  norm  ||m_t - m_{t-1}||_inf  computed on normalized messages.
* converged     = residual(t) < tol  (default tol = 1e-6).
* max_iter      = hard cap on synchronous sweeps (default 200).
* oscillating   = not converged (residual still >> tol) and either
                  (a) a limit cycle: message vectors nearly repeat at lag
                  2, 3 or 4 (max|m_t - m_{t-lag}| <= OSC_REL * residual(t)),
                  or (b) the residual stagnates: its mean over the last half
                  of a 12-iteration window is >= 95% of the first half, while
                  fluctuating within the window (peak/trough >= 1.05).
                  Stagnation must persist 5 consecutive checks before we stop.
* stalled       = residual stagnates as above but decreases (nearly)
                  monotonically: no limit cycle, no fluctuation.  It would
                  converge eventually, just not within any practical budget.
* diverged      = residual mean grew by >= DIVERGE_FACTOR over the window.
* damping       = m_t <- (1-damping) * m_computed + damping * m_{t-1};
                  damping in [0, 1) stabilizes strongly coupled graphs.

Numerical guards
----------------
* Negative entries below -NEG_TOL raise NumericalGuardError (a real bug).
* Tiny negatives in [-NEG_TOL, 0) from float round-off are clamped to 0.
* A message whose total mass is zero (or NaN) raises DegenerateMessageError
  instead of emitting a meaningless "distribution".
* The same checks run on beliefs, so contradictory hard evidence is caught
  instead of silently returned.
"""

NEG_TOL = 1e-12
OSC_WINDOW = 12          # residual window for stagnation detection
OSC_MIN_ITER = 20        # never classify before this many sweeps
OSC_REL = 0.05           # limit-cycle: lagged diff vs one-step residual
OSC_RESIDUAL_FLOOR = 100.0   # only flag when residual > floor * tol
OSC_STAGNANT = 0.95      # half-window mean ratio considered "not shrinking"
OSC_FLUCT = 1.05         # peak/trough inside window -> genuine oscillation
OSC_PERSIST = 5          # consecutive stagnant checks before stopping
DIVERGE_FACTOR = 10.0


class NumericalGuardError(RuntimeError):
    pass


class DegenerateMessageError(NumericalGuardError):
    pass


class BPResult:
    def __init__(self, marginals, residuals, status, iterations, damping, tol):
        self.marginals = marginals
        self.residuals = residuals
        self.status = status
        self.iterations = iterations
        self.damping = damping
        self.tol = tol

    @property
    def converged(self):
        return self.status == "converged"

    @property
    def trustworthy(self):
        """Marginals are only meaningful when BP actually converged."""
        return self.converged

    def max_error_vs(self, exact_marginals):
        """Max L-inf error of marginals against a dict var -> list of floats."""
        err = 0.0
        for var, exact in exact_marginals.items():
            approx = self.marginals[var]
            err = max(err, max(abs(a - b) for a, b in zip(approx, exact)))
        return err


def _sanitize_message(values, context):
    total = 0.0
    for v in values:
        if v >= 0.0:
            total += v
        elif v >= -NEG_TOL:
            pass  # float round-off: clamped to 0 below
        elif v != v:  # NaN
            raise DegenerateMessageError(f"NaN in message {context}")
        else:
            raise NumericalGuardError(
                f"negative entry {v!r} in message {context} (beyond round-off)"
            )
    if not (total > 0.0):
        raise DegenerateMessageError(
            f"zero-mass message {context}: cannot normalize; "
            "likely contradictory hard evidence"
        )
    inv = 1.0 / total
    return [v * inv if v > 0.0 else 0.0 for v in values]


def _combine_damped(msg, old, damping):
    """Convex combination of two normalized non-negative messages.

    The result is already normalized and non-negative, so it needs no
    further sanitization.
    """
    a = 1.0 - damping
    return [a * m + damping * o for m, o in zip(msg, old)]


def _diff2(msg, old):
    """L-inf distance; specialized for the common binary case."""
    if len(msg) == 2:
        d0 = msg[0] - old[0]
        d1 = msg[1] - old[1]
        if d0 < 0:
            d0 = -d0
        if d1 < 0:
            d1 = -d1
        return d0 if d0 > d1 else d1
    return max(abs(a - b) for a, b in zip(msg, old))


class BeliefPropagation:
    def __init__(self, graph, damping=0.0, tol=1e-6, max_iter=200):
        if not 0.0 <= damping < 1.0:
            raise ValueError("damping must be in [0, 1)")
        self.graph = graph
        self.damping = damping
        self.tol = tol
        self.max_iter = max_iter
        self._build()

    def _build(self):
        g = self.graph
        self.variables = g.variables()
        # factor adjacency: factor idx -> list of vars; var -> list of factor idx
        self.factor_vars = [list(f.vars) for f in g.factors]
        self.var_factors = {v: list(idxs) for v, idxs in g.var_to_factors.items()}
        # messages keyed by (var, factor_idx)
        self.v2f = {}
        self.f2v = {}
        uniform = {}
        for v in self.variables:
            c = g.cardinality[v]
            uniform[v] = [1.0 / c] * c
        for v, fidxs in self.var_factors.items():
            for fi in fidxs:
                self.v2f[(v, fi)] = list(uniform[v])
                self.f2v[(fi, v)] = list(uniform[v])

    # ---- message computations -------------------------------------------
    def _sweep_v2f(self):
        """All variable->factor messages in one pass.

        Per variable: one prefix/suffix product each, so the whole sweep is
        O(sum_v deg(v) * card(v)), not O(sum_v deg(v)^2 * card(v)).
        """
        new_msgs = {}
        for var, incoming in self.var_factors.items():
            card = self.graph.cardinality[var]
            n = len(incoming)
            if n == 1:
                new_msgs[(var, incoming[0])] = [1.0] * card
                continue
            msgs = [self.f2v[(fi, var)] for fi in incoming]
            prefix = [[1.0] * card]
            for m in msgs:
                prev = prefix[-1]
                prefix.append([prev[k] * m[k] for k in range(card)])
            suffix = [None] * (n + 1)
            suffix[n] = [1.0] * card
            for pos in range(n - 1, -1, -1):
                m = msgs[pos]
                nxt = suffix[pos + 1]
                suffix[pos] = [nxt[k] * m[k] for k in range(card)]
            for pos, fi in enumerate(incoming):
                pre, suf = prefix[pos], suffix[pos + 1]
                new_msgs[(var, fi)] = [pre[k] * suf[k] for k in range(card)]
        return new_msgs

    def _compute_f2v_general(self, factor_idx, target):
        factor = self.graph.factors[factor_idx]
        scope = factor.vars
        card = self.graph.cardinality[target]
        out = [0.0] * card
        strides = []
        stride = 1
        for c in reversed(factor.cards):
            strides.append(stride)
            stride *= c
        strides.reverse()
        others = [v for v in scope if v != target]
        incoming = [self.v2f[(v, factor_idx)] for v in others]
        cards = factor.cards
        for idx, fval in enumerate(factor.values):
            if fval == 0.0:
                continue
            w = fval
            rem = idx
            oi = 0
            tval = 0
            for pos, v in enumerate(scope):
                x = rem // strides[pos]
                rem %= strides[pos]
                if v == target:
                    tval = x
                else:
                    w *= incoming[oi][x]
                    oi += 1
                    if w == 0.0:
                        break
            else:
                out[tval] += w
        return out

    # ---- main loop -------------------------------------------------------
    def run(self):
        residuals = []
        status = "max_iter_exceeded"
        stagnant_streak = 0
        history = []  # recent (v2f, f2v) snapshots for oscillation detection
        for iteration in range(1, self.max_iter + 1):
            residual = 0.0
            # variable -> factor (batched prefix/suffix products)
            raw_v2f = self._sweep_v2f()
            new_v2f = {}
            for key, raw in raw_v2f.items():
                old = self.v2f[key]
                msg = _sanitize_message(raw, f"{key[0]}->F{key[1]}")
                if self.damping:
                    msg = _combine_damped(msg, old, self.damping)
                d = _diff2(msg, old)
                if d > residual:
                    residual = d
                new_v2f[key] = msg
            self.v2f = new_v2f
            # factor -> variable (unary / pairwise fast paths)
            new_f2v = {}

            def store(target, fi, raw):
                nonlocal residual
                old = self.f2v[(fi, target)]
                msg = _sanitize_message(raw, f"F{fi}->{target}")
                if self.damping:
                    msg = _combine_damped(msg, old, self.damping)
                d = _diff2(msg, old)
                if d > residual:
                    residual = d
                new_f2v[(fi, target)] = msg

            for fi, factor in enumerate(self.graph.factors):
                scope = factor.vars
                if len(scope) == 1:
                    store(scope[0], fi, list(factor.values))
                elif len(scope) == 2:
                    u, w = scope
                    mu = self.v2f[(u, fi)]
                    mw = self.v2f[(w, fi)]
                    vals = factor.values
                    cu, cw = factor.cards
                    if cu == 2 and cw == 2:
                        # psi(xu, xw), big-endian values[xu*cw + xw]
                        store(w, fi, [vals[0] * mu[0] + vals[2] * mu[1],
                                      vals[1] * mu[0] + vals[3] * mu[1]])
                        store(u, fi, [vals[0] * mw[0] + vals[1] * mw[1],
                                      vals[2] * mw[0] + vals[3] * mw[1]])
                    else:
                        out_w = [0.0] * cw
                        out_u = [0.0] * cu
                        for xu in range(cu):
                            base = xu * cw
                            mu_x = mu[xu]
                            for xw in range(cw):
                                v = vals[base + xw]
                                if v:
                                    out_w[xw] += v * mu_x
                                    out_u[xu] += v * mw[xw]
                        store(w, fi, out_w)
                        store(u, fi, out_u)
                else:
                    for target in scope:
                        store(target, fi, self._compute_f2v_general(fi, target))
            self.f2v = new_f2v
            residuals.append(residual)
            if residual < self.tol:
                status = "converged"
                break
            # failure classification (only when far from converged)
            if iteration >= OSC_MIN_ITER and residual > OSC_RESIDUAL_FLOOR * self.tol:
                verdict = self._classify(residuals, history, residual)
                if verdict == "oscillating_cycle":
                    status = "oscillating"
                    break
                if verdict == "diverged":
                    status = "diverged"
                    break
                if verdict in ("oscillating", "stalled"):
                    stagnant_streak += 1
                    if stagnant_streak >= OSC_PERSIST:
                        status = verdict
                        break
                else:
                    stagnant_streak = 0
            history.append((self.v2f, self.f2v))
            if len(history) > 4:
                history.pop(0)
        marginals = self._beliefs()
        return BPResult(marginals, residuals, status, len(residuals),
                        self.damping, self.tol)

    def _classify(self, residuals, history, residual):
        # (a) exact limit cycle at small lag
        for lag in (2, 3, 4):
            if len(history) >= lag and self._snapshot_diff(history[-lag]) \
                    <= OSC_REL * residual:
                return "oscillating_cycle"
        if len(residuals) < OSC_WINDOW:
            return None
        window = residuals[-OSC_WINDOW:]
        half = OSC_WINDOW // 2
        first = sum(window[:half]) / half
        second = sum(window[half:]) / half
        if second >= DIVERGE_FACTOR * first:
            return "diverged"
        if second >= OSC_STAGNANT * first:
            if max(window) >= OSC_FLUCT * min(window):
                return "oscillating"
            return "stalled"
        return None

    def _snapshot_diff(self, snapshot):
        v2f, f2v = snapshot
        diff = 0.0
        for key, msg in self.v2f.items():
            for a, b in zip(msg, v2f[key]):
                d = abs(a - b)
                if d > diff:
                    diff = d
        for key, msg in self.f2v.items():
            for a, b in zip(msg, f2v[key]):
                d = abs(a - b)
                if d > diff:
                    diff = d
        return diff

    def _beliefs(self):
        marginals = {}
        for v in self.variables:
            card = self.graph.cardinality[v]
            belief = [1.0] * card
            for fi in self.var_factors[v]:
                msg = self.f2v[(fi, v)]
                for k in range(card):
                    belief[k] *= msg[k]
            marginals[v] = _sanitize_message(belief, f"belief({v})")
        return marginals
