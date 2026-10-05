"""和积置信传播（Loopy Belief Propagation），仅标准库。

收敛判定（默认，可在 BPConfig 中调整）：
  - 残差口径：每轮同步更新后，取全部 factor->var 消息在 L1 归一化后的
    L_inf 最大逐分量差，residual = max_f,i,x |m_new(x) - m_old(x)|。
  - 收敛阈值：residual < tol（默认 1e-9）。
  - 最大轮数：max_iter（默认 1000），超限判为不收敛。

不收敛处置：
  - 若检测到残差呈周期 2 振荡，标记 status="oscillating"；
  - 其余超限情形标记 status="not_converged"；
  - 两种情形均返回历史残差最小一轮对应的消息所算出的边缘（best-effort），
    并在 result.warnings 中说明，调用方不应将其当作精确结果。

振荡对策：阻尼 m <- normalize((1-damping)*m_new + damping*m_old)；
开启 adaptive_damping 后，残差连续回升时自动加大阻尼（<= max_damping）。

消息防护：
  - 消息分量裁剪到 [0, +inf)，负值（数值误差）截为 0；
  - 归一化时若消息全零（确定性因子导致的退化消息），回退为均匀分布并
    记录 warning，绝不输出 NaN/负概率；
  - 变量边缘若乘积和为 0（变量被互斥约束夹死），同样回退均匀并告警。
"""

import math

_EPS = 1e-300          # 防止 log/除零的极小正数
_UNIFORM_FALLBACK = "消息全零（退化），已回退为均匀分布"


class BPConfig:
    def __init__(
        self,
        tol=1e-9,
        max_iter=1000,
        damping=0.0,
        adaptive_damping=True,
        max_damping=0.9,
        track_beliefs=True,
        belief_track_max_vars=2000,
    ):
        if not 0.0 < tol:
            raise ValueError("tol 必须为正")
        if max_iter < 1:
            raise ValueError("max_iter 必须 >= 1")
        if not 0.0 <= damping < 1.0:
            raise ValueError("damping 必须在 [0, 1)")
        if not 0.0 <= max_damping < 1.0:
            raise ValueError("max_damping 必须在 [0, 1)")
        self.tol = tol
        self.max_iter = max_iter
        self.damping = damping
        self.adaptive_damping = adaptive_damping
        self.max_damping = max_damping
        self.track_beliefs = track_beliefs
        self.belief_track_max_vars = belief_track_max_vars


class BPResult:
    def __init__(self):
        self.status = "not_run"      # converged / oscillating / not_converged
        self.converged = False
        self.iterations = 0
        self.residuals = []          # 每轮残差（收敛曲线数据）
        self.final_residual = math.inf
        self.beliefs = []            # 每个变量的边缘分布（list[list[float]]）
        self.damping_used = 0.0
        self.warnings = []
        self.degenerate_messages = 0  # 触发全零回退的消息条数

    def summary(self):
        return (
            "status=%s iters=%d final_residual=%.3e damping=%.2f "
            "degenerate_msgs=%d"
            % (
                self.status,
                self.iterations,
                self.final_residual,
                self.damping_used,
                self.degenerate_messages,
            )
        )


def _normalize_guarded(values, warnings, degenerate_counter):
    """裁剪负值并 L1 归一化；全零时回退均匀分布。返回 (msg, hit_guard)。"""
    total = 0.0
    for i, v in enumerate(values):
        if v < 0.0:
            values[i] = 0.0
            v = 0.0
        total += v
    if total <= 0.0 or not math.isfinite(total):
        degenerate_counter[0] += 1
        if len(warnings) < 20:
            warnings.append(_UNIFORM_FALLBACK)
        k = len(values)
        return [1.0 / k] * k, True
    inv = 1.0 / total
    return [v * inv for v in values], False


def _var_to_factor_messages(graph, f2v, unary_beliefs):
    """由 factor->var 消息计算全部 var->factor 消息。

    利用“节点信念 = 全部入消息乘积”做除法加速；当入消息为 0
    （除法不可用）时回退到直接连乘，保证零消息下依然正确。
    """
    cards = graph.cardinalities
    incidence = graph._var_factors
    v2f = {}
    for v in range(graph.num_vars):
        fis = incidence[v]
        k = cards[v]
        if len(fis) == 1:
            # 叶变量：var->factor 消息为均匀，不影响结果
            v2f[(v, fis[0])] = [1.0 / k] * k
            continue
        belief = list(unary_beliefs[v])
        for fi in fis:
            incoming = f2v[(fi, v)]
            out = [0.0] * k
            need_direct = False
            for x in range(k):
                m = incoming[x]
                if m > _EPS:
                    out[x] = belief[x] / m
                else:
                    need_direct = True
                    break
            if need_direct:
                for x in range(k):
                    prod = 1.0
                    for gj in fis:
                        if gj != fi:
                            prod *= f2v[(gj, v)][x]
                    out[x] = prod
            v2f[(v, fi)] = out
    return v2f


def _factor_to_var_messages(graph, v2f, warnings, degenerate_counter):
    """由 var->factor 消息计算全部 factor->var 消息（未归一化前防护）。"""
    f2v_new = {}
    for fi, fac in enumerate(graph.factors):
        vars_ = fac.var_indices
        table = fac.table
        arity = fac.arity
        in_msgs = [v2f[(v, fi)] for v in vars_]
        if arity == 1:
            # 一元因子：消息即势函数本身
            msg, _ = _normalize_guarded(
                list(table), warnings, degenerate_counter
            )
            f2v_new[(fi, vars_[0])] = msg
            continue
        if arity == 2:
            # 成对因子快速路径：O(k^2)
            k0, k1 = fac.cardinalities
            m0, m1 = in_msgs
            out0 = [0.0] * k0
            out1 = [0.0] * k1
            for x0 in range(k0):
                base = x0 * k1
                acc = 0.0
                for x1 in range(k1):
                    w = table[base + x1]
                    if w:
                        acc += w * m1[x1]
                        out1[x1] += w * m0[x0]
                out0[x0] = acc
            f2v_new[(fi, vars_[0])], _ = _normalize_guarded(
                out0, warnings, degenerate_counter
            )
            f2v_new[(fi, vars_[1])], _ = _normalize_guarded(
                out1, warnings, degenerate_counter
            )
            continue
        # 一般高阶因子：枚举表项
        cards = fac.cardinalities
        outs = [[0.0] * k for k in cards]
        strides = fac._strides
        size = fac._size
        for flat in range(size):
            w = table[flat]
            if not w:
                continue
            rem = flat
            prod = w
            assign = [0] * arity
            skip = False
            for a in range(arity):
                xa = rem // strides[a]
                rem -= xa * strides[a]
                assign[a] = xa
                prod *= in_msgs[a][xa]
                if prod == 0.0:
                    skip = True
                    break
            if skip:
                # 该赋值下连乘已为 0，但仍需把各分量消息为 0 的贡献记 0，
                # 直接 continue 等价（贡献为 0）。
                continue
            for a in range(arity):
                xa = assign[a]
                ma = in_msgs[a][xa]
                if ma > 0.0:
                    outs[a][xa] += prod / ma
        for a, v in enumerate(vars_):
            f2v_new[(fi, v)], _ = _normalize_guarded(
                outs[a], warnings, degenerate_counter
            )
    return f2v_new


def _compute_unary_beliefs(graph, f2v, warnings, degenerate_counter):
    beliefs = []
    for v in range(graph.num_vars):
        k = graph.cardinalities[v]
        prod = [1.0] * k
        for fi in graph._var_factors[v]:
            msg = f2v[(fi, v)]
            for x in range(k):
                prod[x] *= msg[x]
        b, _ = _normalize_guarded(prod, warnings, degenerate_counter)
        beliefs.append(b)
    return beliefs


def _max_message_diff(f2v_new, f2v_old):
    """返回 (残差, 取得残差的消息向量副本)。后者用作振荡探针。"""
    residual = 0.0
    probe = None
    for key, new_msg in f2v_new.items():
        old_msg = f2v_old[key]
        for a, b in zip(new_msg, old_msg):
            d = a - b
            if d < 0.0:
                d = -d
            if d > residual:
                residual = d
                probe = list(new_msg)
    return residual, probe


def _detect_period2(residuals, probes, window=6, rel_tol=1e-3):
    """周期 2 振荡检测。

    两个判据满足其一即判定振荡：
    1. 探针消息（每轮残差最大的消息）满足 m[t] ≈ m[t-2] 且与 m[t-1]
       明显不同——消息在两态间翻转（此时残差曲线可以是平的，单看残差
       无法识别）；
    2. 残差本身满足 r[t] ≈ r[t-2] 且 r[t] 与 r[t-1] 明显不同。
    """
    if len(residuals) < window + 2:
        return False

    def period2(seq, dist):
        for i in range(2, window):
            if dist(seq[i], seq[i - 2]) > rel_tol:
                return False
        for i in range(1, window):
            if dist(seq[i], seq[i - 1]) < 1e-2:
                return False
        return True

    if probes and len(probes) >= window:
        tail_p = probes[-window:]

        def msg_dist(a, b):
            d = max(abs(x - y) for x, y in zip(a, b))
            return d / max(max(a), max(b), 1e-300)

        if period2(tail_p, msg_dist):
            return True

    tail_r = residuals[-window:]

    def res_dist(a, b):
        return abs(a - b) / max(a, b, 1e-300)

    return period2(tail_r, res_dist)


def belief_propagation(graph, config=None):
    """在因子图上运行和积 BP，返回 BPResult。"""
    if config is None:
        config = BPConfig()
    graph.freeze()
    cards = graph.cardinalities
    warnings = []
    degenerate_counter = [0]

    # 初始化：factor->var 均匀；unary 信念均匀
    f2v = {}
    for fi, fac in enumerate(graph.factors):
        for v in fac.var_indices:
            k = cards[v]
            f2v[(fi, v)] = [1.0 / k] * k
    unary = [[1.0 / cards[v]] * cards[v] for v in range(graph.num_vars)]

    damping = config.damping
    track = config.track_beliefs and graph.num_vars <= config.belief_track_max_vars
    residuals = []
    probes = []
    best_residual = math.inf
    best_f2v = None
    status = "not_converged"
    iterations = 0
    rises_in_a_row = 0

    for it in range(1, config.max_iter + 1):
        iterations = it
        v2f = _var_to_factor_messages(graph, f2v, unary)
        f2v_new = _factor_to_var_messages(graph, v2f, warnings, degenerate_counter)

        if damping > 0.0:
            keep = damping
            take = 1.0 - damping
            for key, new_msg in f2v_new.items():
                old_msg = f2v[key]
                for x in range(len(new_msg)):
                    new_msg[x] = take * new_msg[x] + keep * old_msg[x]
                # 阻尼后重新归一化（凸组合和仍为正，防护兜底）
                s = sum(new_msg)
                if s > 0.0:
                    inv = 1.0 / s
                    for x in range(len(new_msg)):
                        new_msg[x] *= inv
                else:
                    k = len(new_msg)
                    for x in range(k):
                        new_msg[x] = 1.0 / k

        residual, probe = _max_message_diff(f2v_new, f2v)
        residuals.append(residual)
        if probe is not None:
            probes.append(probe)
            if len(probes) > 8:
                probes.pop(0)
        f2v = f2v_new
        unary = _compute_unary_beliefs(graph, f2v, warnings, degenerate_counter)

        if track and residual < best_residual:
            best_residual = residual
            best_f2v = f2v

        if residual < config.tol:
            status = "converged"
            break

        # 自适应阻尼：残差连续回升则加大阻尼
        if config.adaptive_damping and len(residuals) >= 2:
            if residuals[-1] > residuals[-2]:
                rises_in_a_row += 1
            else:
                rises_in_a_row = 0
            if rises_in_a_row >= 3 and damping < config.max_damping:
                damping = min(config.max_damping, damping + 0.1)
                rises_in_a_row = 0

    else:
        # 达到 max_iter 未收敛：区分振荡与停滞
        if _detect_period2(residuals, probes):
            status = "oscillating"
        else:
            status = "not_converged"

    result = BPResult()
    result.status = status
    result.converged = status == "converged"
    result.iterations = iterations
    result.residuals = residuals
    result.final_residual = residuals[-1] if residuals else math.inf
    result.damping_used = damping
    result.warnings = warnings
    result.degenerate_messages = degenerate_counter[0]

    if not result.converged:
        result.warnings.append(
            "未收敛（%s）：返回历史残差最小一轮的边缘，仅供参考"
            % status
        )
        if best_f2v is not None:
            unary = _compute_unary_beliefs(
                graph, best_f2v, warnings, degenerate_counter
            )
    result.beliefs = unary
    return result
