"""查询代价估算模型的校准库（仅 Python 标准库）。

代价模型为线性形式：

    cost = c[0] + c[1]*x1 + c[2]*x2 + ... + c[p]*xp

校准方法：带微小岭正则的最小二乘（正规方程直接求解，无迭代，结果确定）。
详细方法说明见 CALIBRATION.md。
"""

from __future__ import annotations

import math
import random
from array import array
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 拒绝原因
# ---------------------------------------------------------------------------

INSUFFICIENT_SAMPLES = "INSUFFICIENT_SAMPLES"   # 样本量不足
DISTRIBUTION_DRIFT = "DISTRIBUTION_DRIFT"       # 采集窗口内分布漂移
DEGENERATE_FEATURES = "DEGENERATE_FEATURES"     # 特征无方差（全为同一取值）
SINGULAR_MATRIX = "SINGULAR_MATRIX"             # 特征共线，正规方程奇异


class CalibrationRejected(Exception):
    """校准被拒绝时抛出。reason 为上面的常量之一。"""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


# ---------------------------------------------------------------------------
# 代价模型
# ---------------------------------------------------------------------------

class CostModel:
    """线性代价模型。coefficients[0] 为截距，其后与各特征一一对应。"""

    def __init__(self, coefficients):
        self.coefficients = [float(c) for c in coefficients]

    @property
    def n_features(self) -> int:
        return len(self.coefficients) - 1

    def predict(self, features) -> float:
        coeffs = self.coefficients
        if len(features) != len(coeffs) - 1:
            raise ValueError(
                f"expected {len(coeffs) - 1} features, got {len(features)}")
        total = coeffs[0]
        for c, x in zip(coeffs[1:], features):
            total += c * x
        return total


# ---------------------------------------------------------------------------
# 误差指标
# ---------------------------------------------------------------------------

@dataclass
class ErrorMetrics:
    n: int
    mae: float
    rmse: float
    mean_qerror: float
    median_qerror: float
    p95_qerror: float

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "mae": self.mae,
            "rmse": self.rmse,
            "mean_qerror": self.mean_qerror,
            "median_qerror": self.median_qerror,
            "p95_qerror": self.p95_qerror,
        }


def _qerror(pred: float, actual: float) -> float:
    pred = max(pred, 1e-9)
    actual = max(actual, 1e-9)
    return pred / actual if pred >= actual else actual / pred


def compute_metrics(model: CostModel, features_flat: array, targets: array,
                    n_features: int) -> ErrorMetrics:
    """在留出集上计算误差指标。features_flat 为按行铺平的 array('d')。"""
    n = len(targets)
    if n == 0:
        raise ValueError("empty evaluation set")
    coeffs = model.coefficients
    abs_sum = 0.0
    sq_sum = 0.0
    qerrors = []
    nf = n_features
    for row in range(n):
        base = row * nf
        pred = coeffs[0]
        for j in range(nf):
            pred += coeffs[j + 1] * features_flat[base + j]
        actual = targets[row]
        err = pred - actual
        abs_sum += abs(err)
        sq_sum += err * err
        qerrors.append(_qerror(pred, actual))
    qerrors.sort()
    mae = abs_sum / n
    rmse = math.sqrt(sq_sum / n)
    mean_q = sum(qerrors) / n
    median_q = qerrors[n // 2] if n % 2 else 0.5 * (qerrors[n // 2 - 1] + qerrors[n // 2])
    p95_q = qerrors[min(n - 1, int(math.ceil(0.95 * n)) - 1)]
    return ErrorMetrics(n=n, mae=mae, rmse=rmse, mean_qerror=mean_q,
                        median_qerror=median_q, p95_qerror=p95_q)


# ---------------------------------------------------------------------------
# 线性代数：正规方程求解（高斯-若尔当消元 + 部分主元，完全确定）
# ---------------------------------------------------------------------------

def _solve_linear(matrix, rhs, col_scales=None, rel_pivot_eps=1e-9):
    """高斯-若尔当消元。主元小于列尺度的 rel_pivot_eps 倍时判定奇异。

    相对阈值使得岭正则无法“救活”共线矩阵：共线列的主元只剩 ridge
    量级，远低于列尺度，从而被可靠拒绝。
    """
    n = len(rhs)
    if col_scales is None:
        col_scales = [max(abs(matrix[r][c]) for r in range(n)) for c in range(n)]
    aug = [list(matrix[i]) + [rhs[i]] for i in range(n)]
    for col in range(n):
        piv = col
        best = abs(aug[col][col])
        for r in range(col + 1, n):
            v = abs(aug[r][col])
            if v > best:
                best = v
                piv = r
        eps = rel_pivot_eps * max(col_scales[col], 1e-300)
        if best < eps:
            raise CalibrationRejected(
                SINGULAR_MATRIX,
                f"column {col} pivot {best:.3e} < {eps:.3e} "
                f"(relative to column scale); features are collinear")
        if piv != col:
            aug[col], aug[piv] = aug[piv], aug[col]
        inv_pivot = 1.0 / aug[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col] * inv_pivot
            if factor == 0.0:
                continue
            row_r = aug[r]
            row_c = aug[col]
            for c in range(col, n + 1):
                row_r[c] -= factor * row_c[c]
    return [aug[i][n] / aug[i][i] for i in range(n)]


# ---------------------------------------------------------------------------
# 分布漂移检测：两样本 KS 检验（时间前一半 vs 后一半）
# ---------------------------------------------------------------------------

def _ks_statistic(xs, ys) -> float:
    xs = sorted(xs)
    ys = sorted(ys)
    n, m = len(xs), len(ys)
    i = j = 0
    d = 0.0
    while i < n and j < m:
        if xs[i] <= ys[j]:
            i += 1
        else:
            j += 1
        diff = abs(i / n - j / m)
        if diff > d:
            d = diff
    return d


def _ks_pvalue(d: float, n: int, m: int) -> float:
    ne = n * m / (n + m)
    lam = (math.sqrt(ne) + 0.12 + 0.11 / math.sqrt(ne)) * d
    if lam <= 0.0:
        return 1.0
    total = 0.0
    for k in range(1, 101):
        term = math.exp(-2.0 * k * k * lam * lam)
        total += (-1.0) ** (k - 1) * term
        if term < 1e-12:
            break
    return max(0.0, min(1.0, 2.0 * total))


# ---------------------------------------------------------------------------
# 校准报告
# ---------------------------------------------------------------------------

@dataclass
class CalibrationReport:
    coefficients: list
    baseline_metrics: ErrorMetrics
    calibrated_metrics: ErrorMetrics
    n_samples: int
    n_train: int
    n_holdout: int
    seed: int
    drift_pvalues: list = field(default_factory=list)

    def improvement(self) -> dict:
        b, c = self.baseline_metrics, self.calibrated_metrics
        return {
            "mae_ratio": c.mae / b.mae if b.mae else float("inf"),
            "rmse_ratio": c.rmse / b.rmse if b.rmse else float("inf"),
            "mean_qerror_ratio": (c.mean_qerror / b.mean_qerror
                                  if b.mean_qerror else float("inf")),
        }


# ---------------------------------------------------------------------------
# 主入口：流式校准
# ---------------------------------------------------------------------------

def calibrate(samples, baseline_coefficients, *, seed=0,
              holdout_fraction=0.2, min_samples=100,
              drift_alpha=1e-3, reservoir_size=2000, ridge=1e-9):
    """对线性代价模型做最小二乘校准。

    参数
    ----
    samples : 可迭代对象，元素为 (features_tuple, actual_cost)。
              只遍历一遍，可接生成器，内存与样本数无关。
    baseline_coefficients : 现有（拍脑袋）系数，含截距，长度 p+1。
    seed : 随机种子，控制留出集划分与蓄水池抽样；同输入同种子结果完全一致。
    holdout_fraction : 留出验证集比例（确定性随机划分）。
    min_samples : 最小样本量，不足则拒绝校准。
    drift_alpha : KS 检验显著性水平，任一特征 p 值低于它即判定漂移并拒绝。
    reservoir_size : 漂移检测用蓄水池大小（与样本总量无关）。
    ridge : 岭正则强度（固定值，保证可复现）。

    返回 CalibrationReport；数据不合格时抛出 CalibrationRejected。
    """
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be in (0, 1)")
    baseline = CostModel(baseline_coefficients)
    n_features = baseline.n_features
    dim = n_features + 1

    rng = random.Random(seed)

    xtx = [[0.0] * dim for _ in range(dim)]   # 训练集 X^T X（含截距列）
    xty = [0.0] * dim                          # 训练集 X^T y
    n_train = 0

    holdout_x = array("d")   # 留出集特征（按行铺平）
    holdout_y = array("d")   # 留出集实际值

    res_idx = array("q")     # 蓄水池：样本序号
    res_x = []               # 蓄水池：特征元组

    n_total = 0
    for row in samples:
        features, actual = row
        if len(features) != n_features:
            raise ValueError(
                f"expected {n_features} features, got {len(features)}")
        # 蓄水池抽样（用于时间维度的漂移检测）
        if n_total < reservoir_size:
            res_idx.append(n_total)
            res_x.append(tuple(features))
        else:
            j = rng.randrange(n_total + 1)
            if j < reservoir_size:
                res_idx[j] = n_total
                res_x[j] = tuple(features)
        # 确定性随机划分：留出 or 训练
        if rng.random() < holdout_fraction:
            holdout_x.extend(features)
            holdout_y.append(actual)
        else:
            v = [1.0]
            v.extend(features)
            for a in range(dim):
                va = v[a]
                xty[a] += va * actual
                row_a = xtx[a]
                for b in range(a, dim):
                    row_a[b] += va * v[b]
            n_train += 1
        n_total += 1

    # ---- 拒绝条件 1：样本量不足 -------------------------------------------
    min_holdout = max(10, dim * 2)
    if n_total < min_samples or n_train < dim * 2 or len(holdout_y) < min_holdout:
        raise CalibrationRejected(
            INSUFFICIENT_SAMPLES,
            f"need >= {min_samples} samples (and >= {min_holdout} holdout), "
            f"got {n_total} total / {n_train} train / {len(holdout_y)} holdout")

    # ---- 拒绝条件 2：特征退化（全为同一取值） ------------------------------
    means = [xtx[0][j] / n_train for j in range(dim)]
    variances = []
    for j in range(1, dim):
        var = xtx[j][j] / n_train - means[j] ** 2
        variances.append(var)
        if var <= 1e-12 * max(1.0, means[j] ** 2):
            raise CalibrationRejected(
                DEGENERATE_FEATURES,
                f"feature {j - 1} has (near-)zero variance "
                f"(var={var:.3e}); cannot identify its coefficient")

    # ---- 拒绝条件 3：分布漂移（时间前半段 vs 后半段，逐特征 KS 检验） ------
    order = sorted(range(len(res_idx)), key=lambda k: res_idx[k])
    half = len(order) // 2
    drift_pvalues = []
    for jfeat in range(n_features):
        early = [res_x[k][jfeat] for k in order[:half]]
        late = [res_x[k][jfeat] for k in order[half:]]
        d = _ks_statistic(early, late)
        drift_pvalues.append(_ks_pvalue(d, len(early), len(late)))
    worst = min(drift_pvalues) if drift_pvalues else 1.0
    if worst < drift_alpha:
        bad = drift_pvalues.index(worst)
        raise CalibrationRejected(
            DISTRIBUTION_DRIFT,
            f"feature {bad} drifted between first and second half of the "
            f"collection window (KS p-value {worst:.3e} < {drift_alpha:.1e})")

    # ---- 求解：(X^T X + ridge*I) beta = X^T y ------------------------------
    for a in range(dim):
        for b in range(a):
            xtx[a][b] = xtx[b][a]
    col_scales = [max(abs(xtx[r][c]) for r in range(dim)) for c in range(dim)]
    for a in range(dim):
        xtx[a][a] += ridge
    coefficients = _solve_linear(xtx, xty, col_scales=col_scales)

    # ---- 留出集验证：校准前 vs 校准后 --------------------------------------
    baseline_metrics = compute_metrics(baseline, holdout_x, holdout_y, n_features)
    calibrated_model = CostModel(coefficients)
    calibrated_metrics = compute_metrics(calibrated_model, holdout_x, holdout_y,
                                         n_features)

    return CalibrationReport(
        coefficients=coefficients,
        baseline_metrics=baseline_metrics,
        calibrated_metrics=calibrated_metrics,
        n_samples=n_total,
        n_train=n_train,
        n_holdout=len(holdout_y),
        seed=seed,
        drift_pvalues=drift_pvalues,
    )


# ---------------------------------------------------------------------------
# 演示
# ---------------------------------------------------------------------------

def _demo_samples(n, seed):
    rng = random.Random(seed)
    true = [5.0, 1.0, 8.0, 0.02, 0.05]
    for _ in range(n):
        x = (rng.lognormvariate(3.0, 1.0),
             rng.lognormvariate(1.0, 1.2),
             rng.lognormvariate(6.0, 1.0),
             rng.lognormvariate(3.0, 1.0))
        noise = rng.lognormvariate(0.0, 0.3)
        y = (true[0] + sum(c * v for c, v in zip(true[1:], x))) * noise
        yield x, y


def _print_report(report):
    def fmt(m):
        return (f"MAE={m.mae:10.4f}  RMSE={m.rmse:10.4f}  "
                f"meanQ={m.mean_qerror:6.3f}  medianQ={m.median_qerror:6.3f}  "
                f"p95Q={m.p95_qerror:6.3f}")
    print(f"samples={report.n_samples} train={report.n_train} "
          f"holdout={report.n_holdout} seed={report.seed}")
    print(f"  before: {fmt(report.baseline_metrics)}")
    print(f"  after : {fmt(report.calibrated_metrics)}")
    print(f"  coefficients: {[round(c, 6) for c in report.coefficients]}")


if __name__ == "__main__":
    baseline = [10.0, 2.0, 4.0, 0.05, 0.01]  # 拍脑袋系数
    report = calibrate(_demo_samples(50_000, seed=1), baseline, seed=42)
    _print_report(report)
