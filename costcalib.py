"""costcalib: 查询代价估算模型的系数校准库（仅 Python 标准库）。

模型形式: cost = w[0] + w[1]*x1 + ... + w[k]*xk   （线性代价模型）
校准方法: 普通最小二乘 (OLS)，见 CALIBRATION.md。
"""
from __future__ import annotations

import math
import random
from array import array
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional, Sequence, Tuple

# 校准被拒绝的原因（稳定字符串，便于测试与监控）
REASON_INSUFFICIENT = "insufficient_samples"
REASON_DEGENERATE = "degenerate_features"
REASON_SINGULAR = "singular_system"
REASON_DRIFT = "distribution_drift"
REASON_NO_IMPROVEMENT = "no_holdout_improvement"

_CONST_VAR_RTOL = 1e-14   # 方差相对阈值：低于则视为常量特征
_PIVOT_EPS = 1e-12        # 高斯消元主元阈值
_DRIFT_STRIDE = 8         # 漂移检测抽样步长
_DRIFT_CAP = 131072       # 漂移检测样本上限
_PSI_BINS = 10            # PSI 直方图桶数


class CalibrationRejected(Exception):
    """校准被拒绝时抛出（仅在使用 calibrate_or_raise 时）。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Metrics:
    """留出集上的误差指标。"""
    n: int
    mae: float
    rmse: float
    smape: float   # 对称平均绝对百分比误差，避免除零
    r2: float


@dataclass(frozen=True)
class CalibrationResult:
    status: str                      # "calibrated" 或 "rejected"
    reason: str                      # 拒绝原因；成功时为 "ok"
    coefficients: Tuple[float, ...]  # 最终应使用的系数（拒绝时等于原系数）
    fitted_coefficients: Optional[Tuple[float, ...]]  # 拟合出的系数（未拟合为 None）
    n_samples: int
    n_train: int
    n_holdout: int
    psi_max: Optional[float]         # 漂移检测的最大 PSI
    before: Optional[Metrics]        # 校准前（原系数）留出集指标
    after: Optional[Metrics]         # 校准后（新系数）留出集指标


def _solve(a: list, b: list) -> list:
    """Gauss-Jordan 消元（部分主元）解 Ax=b。完全确定性。"""
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = col
        best = abs(m[col][col])
        for r in range(col + 1, n):
            v = abs(m[r][col])
            if v > best:
                best = v
                piv = r
        if best < _PIVOT_EPS:
            raise ArithmeticError("singular matrix")
        if piv != col:
            m[col], m[piv] = m[piv], m[col]
        inv = 1.0 / m[col][col]
        for r in range(n):
            if r == col:
                continue
            f = m[r][col] * inv
            if f != 0.0:
                row_r = m[r]
                row_c = m[col]
                for c in range(col, n + 1):
                    row_r[c] -= f * row_c[c]
    return [m[i][n] / m[i][i] for i in range(n)]


def _metrics(feats: array, target: array, k: int, w: Sequence[float]) -> Metrics:
    n = len(target)
    if n == 0:
        return Metrics(0, math.nan, math.nan, math.nan, math.nan)
    sae = 0.0
    sse = 0.0
    ssape = 0.0
    sy = 0.0
    syy = 0.0
    stride = k
    for i in range(n):
        base = i * stride
        pred = w[0]
        for j in range(k):
            pred += w[j + 1] * feats[base + j]
        y = target[i]
        err = pred - y
        sae += abs(err)
        sse += err * err
        denom = abs(y) + abs(pred)
        if denom > 0.0:
            ssape += 2.0 * abs(err) / denom
        sy += y
        syy += y * y
    var = syy - sy * sy / n
    r2 = 1.0 - sse / var if var > 0.0 else (1.0 if sse == 0.0 else 0.0)
    return Metrics(n, sae / n, math.sqrt(sse / n), ssape / n, r2)


def _psi(hist_a: Sequence[int], hist_b: Sequence[int]) -> float:
    """两个直方图之间的 Population Stability Index。"""
    na = sum(hist_a)
    nb = sum(hist_b)
    bins = len(hist_a)
    if na == 0 or nb == 0:
        return 0.0
    psi = 0.0
    for ca, cb in zip(hist_a, hist_b):
        pa = (ca + 0.5) / (na + 0.5 * bins)
        pb = (cb + 0.5) / (nb + 0.5 * bins)
        psi += (pa - pb) * math.log(pa / pb)
    return psi


def _drift_psi(drift_feats: array, k: int) -> float:
    """按到达顺序把漂移样本切成前后两半，逐特征计算 PSI，返回最大值。"""
    n = len(drift_feats) // k
    half = n // 2
    if half < 25:
        return 0.0  # 样本太少时不做漂移判定（样本量守卫会先拦截）
    psi_max = 0.0
    for j in range(k):
        lo = math.inf
        hi = -math.inf
        for i in range(n):
            v = drift_feats[i * k + j]
            if v < lo:
                lo = v
            if v > hi:
                hi = v
        if not (hi > lo):
            continue  # 常量特征无漂移可言
        width = (hi - lo) / _PSI_BINS
        ha = [0] * _PSI_BINS
        hb = [0] * _PSI_BINS
        for i in range(n):
            v = drift_feats[i * k + j]
            b = int((v - lo) / width)
            if b >= _PSI_BINS:
                b = _PSI_BINS - 1
            if i < half:
                ha[b] += 1
            else:
                hb[b] += 1
        p = _psi(ha, hb)
        if p > psi_max:
            psi_max = p
    return psi_max


def calibrate(
    samples: Iterable[Tuple[Sequence[float], float]],
    coefficients: Sequence[float],
    *,
    seed: int = 0,
    holdout_ratio: float = 0.2,
    min_samples: int = 1000,
    psi_threshold: float = 0.25,
    ridge: float = 1e-8,
) -> CalibrationResult:
    """用实测数据校准线性代价模型系数。

    参数:
        samples: 可迭代对象，元素为 (features, actual_cost)。
        coefficients: 当前使用的系数 [w0, w1, ..., wk]（含截距）。
        seed: 训练/留出集划分随机种子；同输入同种子结果完全一致。
        holdout_ratio: 留出验证集比例。
        min_samples: 低于该样本量直接拒绝校准。
        psi_threshold: 任一特征 PSI 超过该值判为分布漂移，拒绝校准。
        ridge: 岭正则相对强度，保证法方程可解且结果确定。

    返回 CalibrationResult；拒绝时 coefficients 为原系数。
    """
    w0 = [float(c) for c in coefficients]
    k = len(w0) - 1
    if k < 1:
        raise ValueError("coefficients 至少包含截距和一个特征系数")
    if not (0.0 < holdout_ratio < 1.0):
        raise ValueError("holdout_ratio 必须在 (0, 1) 内")

    dim = k + 1
    gram = [[0.0] * dim for _ in range(dim)]  # X^T X（含截距列）
    vec = [0.0] * dim                          # X^T y
    fsum = [0.0] * k
    fsq = [0.0] * k
    n_train = 0
    n_total = 0

    hold_feats = array("d")
    hold_target = array("d")
    drift_feats = array("d")

    rng = random.Random(seed)
    for feats, actual in samples:
        if len(feats) != k:
            raise ValueError(f"特征维度 {len(feats)} 与系数维度 {k} 不一致")
        y = float(actual)
        if rng.random() < holdout_ratio:
            hold_feats.extend(float(v) for v in feats)
            hold_target.append(y)
        else:
            row = [1.0] + [float(v) for v in feats]
            for i in range(dim):
                xi = row[i]
                gi = gram[i]
                for j in range(i, dim):
                    gi[j] += xi * row[j]
                vec[i] += xi * y
            for j in range(k):
                v = row[j + 1]
                fsum[j] += v
                fsq[j] += v * v
            n_train += 1
        if n_total % _DRIFT_STRIDE == 0 and len(drift_feats) < _DRIFT_CAP * k:
            drift_feats.extend(float(v) for v in feats)
        n_total += 1

    def reject(reason: str, psi_max: Optional[float] = None,
               before: Optional[Metrics] = None) -> CalibrationResult:
        return CalibrationResult(
            status="rejected", reason=reason, coefficients=tuple(w0),
            fitted_coefficients=None, n_samples=n_total, n_train=n_train,
            n_holdout=len(hold_target), psi_max=psi_max, before=before, after=None)

    # ---- 守卫 1：样本量 ----
    if n_total < min_samples or n_train < dim + 1 or len(hold_target) < 10:
        return reject(REASON_INSUFFICIENT)

    # ---- 守卫 2：分布漂移（前后两半流式样本的 PSI）----
    psi_max = _drift_psi(drift_feats, k)
    if psi_max >= psi_threshold:
        return reject(REASON_DRIFT, psi_max=psi_max)

    # ---- 常量特征处理：冻结原系数，其贡献从法方程中代数消去 ----
    keep = [0]  # 截距永远保留
    frozen: dict = {}
    for j in range(k):
        mean = fsum[j] / n_train
        var = fsq[j] / n_train - mean * mean
        scale = max(1.0, mean * mean)
        if var <= _CONST_VAR_RTOL * scale:
            frozen[j + 1] = w0[j + 1]
        else:
            keep.append(j + 1)
    if len(keep) == 1 and frozen:
        # 所有特征都是常量：无法拟合任何斜率
        return reject(REASON_DEGENERATE, psi_max=psi_max)

    # 对称化 gram
    for i in range(dim):
        for j in range(i):
            gram[i][j] = gram[j][i]

    # 从法方程中消去冻结列：b_j -= G[j][i] * w_i
    for i, wi in frozen.items():
        for j in range(dim):
            vec[j] -= gram[j][i] * wi

    sub_gram = [[gram[i][j] for j in keep] for i in keep]
    sub_vec = [vec[i] for i in keep]

    # ---- 岭正则 + 求解（目标函数见 CALIBRATION.md）----
    m = len(keep)
    for i in range(m):
        sub_gram[i][i] *= 1.0 + ridge
    try:
        sol = _solve(sub_gram, sub_vec)
    except ArithmeticError:
        return reject(REASON_SINGULAR, psi_max=psi_max)

    w_new = list(w0)
    for idx, pos in enumerate(keep):
        w_new[pos] = sol[idx]

    # ---- 留出集验证：校准前后对比，无改进则拒绝 ----
    before = _metrics(hold_feats, hold_target, k, w0)
    after = _metrics(hold_feats, hold_target, k, w_new)
    if not (after.rmse < before.rmse):
        return CalibrationResult(
            status="rejected", reason=REASON_NO_IMPROVEMENT,
            coefficients=tuple(w0), fitted_coefficients=tuple(w_new),
            n_samples=n_total, n_train=n_train, n_holdout=len(hold_target),
            psi_max=psi_max, before=before, after=after)

    return CalibrationResult(
        status="calibrated", reason="ok", coefficients=tuple(w_new),
        fitted_coefficients=tuple(w_new), n_samples=n_total, n_train=n_train,
        n_holdout=len(hold_target), psi_max=psi_max, before=before, after=after)


def calibrate_or_raise(samples, coefficients, **kwargs) -> Tuple[float, ...]:
    """校准成功返回新系数，被拒绝时抛 CalibrationRejected。"""
    result = calibrate(samples, coefficients, **kwargs)
    if result.status != "calibrated":
        raise CalibrationRejected(result.reason)
    return result.coefficients
