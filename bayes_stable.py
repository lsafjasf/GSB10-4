"""bayes_stable: 对数域数值稳定的贝叶斯更新库（仅标准库，Python 3.8+）。

模型
----
离散假设空间 H = {h_0, ..., h_{K-1}}，先验 p(h_i)，观测 x 的似然 L_i = p(x|h_i)。
贝叶斯更新：

    p(h_i | x) = p(h_i) * L_i / Z,   Z = sum_j p(h_j) * L_j

朴素实现在概率域连乘，K 次更新后 p(h) * prod(L) 会下溢成 0.0（double 最小
正规数约 2.2e-308），归一化得到 0/0 = nan，分布彻底失去意义。

本库的做法
----------
1. 全部状态保存在对数域：内部维护未归一化对数权重
       s_i = log p(h_i) + sum_t log L_i^{(t)}
   每次更新只做一次加法 s_i += log L_i，不存在连乘，因此不会下溢。
   似然为 0 时 log L = -inf，表示该假设被永久排除，这在数学上是精确的。
2. 归一化用 log-sum-exp，先减去最大值再 exp，避免 exp 上溢：
       log Z = m + log(sum_i exp(s_i - m)),  m = max_i s_i
       log p(h_i|x) = s_i - log Z
   对数后验是唯一权威输出，任何情况下都有效。
3. 只有用户显式把对数概率转回线性概率（result.prob / prob_or_zero）时，
   才可能出现 exp 下溢（log p < ~-745）。此时库**不会**静默返回 0.0：
   访问 result.prob 抛出 PosteriorUnderflowError，记录进 underflow_log，
   并给出 PosteriorUnderflowRecord（假设、log p、相对最大值的差距）。
   需要线性数组做报表时用 result.prob_or_zero()，下溢项填 0.0 且
   result.has_underflow 为 True，调用方必须检查该标志。

退化输入的确定行为
------------------
- 先验含 0：合法，log 0 = -inf，该假设永久排除（观测也无法复活它）。
- 先验全 0 / 先验和 <= 0 / 空先验：构造时抛 DegeneratePriorError。
- 先验含负数、NaN、+inf：构造时抛 ValueError。
- 某次观测下全部假设似然为 0（log Z = -inf，证据为零）：
  抛 ZeroEvidenceError，状态回滚到本次观测之前。
- 似然含负数、NaN、+inf：抛 ValueError，状态不变。
- 单个似然为 0：合法，对应假设永久排除。

精度监护
--------
未归一化对数状态的最大跨度超过 _SPAN_WARN（1e12）时记录
"precision-loss-guard" 警告：double 只有约 15-16 位有效数字，
跨度继续增大时小权重项的加法会失去意义（远早于任何下溢）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

__all__ = [
    "BayesError",
    "DegeneratePriorError",
    "ZeroEvidenceError",
    "PosteriorUnderflowError",
    "StabilityWarningRecord",
    "PosteriorUnderflowRecord",
    "PosteriorResult",
    "BayesFilter",
    "logsumexp",
    "naive_linear_update",
]

# math.exp(x) 在 x < ~-745.13 时直接下溢为 0.0（denormal 边界）
LOG_EXP_UNDERFLOW = -745.1332191019411
# 未归一化对数状态跨度超过该值时，double 加法开始丢失小权重项
_SPAN_WARN = 1e12


class BayesError(Exception):
    """本库所有异常的基类。"""


class DegeneratePriorError(BayesError):
    """先验退化：全零、和为零、空或含非法值。"""


class ZeroEvidenceError(BayesError):
    """某次观测下所有假设的似然都为零，证据 Z = 0，后验无定义。状态已回滚。"""


class PosteriorUnderflowError(BayesError):
    """把对数后验转回线性概率时发生 exp 下溢；拒绝静默返回 0.0。"""


@dataclass(frozen=True)
class StabilityWarningRecord:
    """精度监护警告（状态跨度过大，加法开始失去意义）。"""

    kind: str
    step: int
    span: float
    detail: str


@dataclass(frozen=True)
class PosteriorUnderflowRecord:
    """一次后验概率 exp 下溢的记录。"""

    label: str
    index: int
    log_prob: float
    log_gap_to_max: float  # log_prob - max(log_prob)，衡量相对差距


def logsumexp(log_values: Sequence[float]) -> float:
    """稳定的 log(sum(exp(v)))：先减最大值再 exp。

    - 全部 -inf -> 返回 -inf（数学上 log 0）。
    - 含 NaN -> 抛 ValueError（NaN 是上游 bug，不应静默传播）。
    """
    m = -math.inf
    for v in log_values:
        if math.isnan(v):
            raise ValueError("logsumexp 收到 NaN")
        if v > m:
            m = v
    if m == -math.inf:
        return -math.inf
    if m == math.inf:
        return math.inf
    total = 0.0
    for v in log_values:
        total += math.exp(v - m)
    return m + math.log(total)


def _validate_loglik(log_lik: Sequence[float]) -> None:
    for v in log_lik:
        if math.isnan(v) or v == math.inf:
            raise ValueError(f"非法对数似然 {v!r}：允许 [-inf, 0] 或任意 <= 0 的有限值/负值")


def _softmax(log_values: Sequence[float]) -> List[float]:
    """log-sum-exp 归一化；调用方保证至少一个有限值。"""
    m = max(log_values)
    exps = [math.exp(v - m) for v in log_values]
    z = sum(exps)
    return [e / z for e in exps]


@dataclass
class PosteriorResult:
    """一次后验查询的结果。对数概率是唯一权威输出。"""

    labels: Tuple[str, ...]
    log_prob: Tuple[float, ...]
    log_evidence: float  # 全部观测的总对数证据 log p(x_1..x_n)
    step: int
    _underflow: Tuple[PosteriorUnderflowRecord, ...] = field(repr=False, default=())

    @property
    def has_underflow(self) -> bool:
        return bool(self._underflow)

    @property
    def underflow_records(self) -> Tuple[PosteriorUnderflowRecord, ...]:
        return self._underflow

    @property
    def prob(self) -> List[float]:
        """线性概率。若有任何一项 exp 下溢，抛 PosteriorUnderflowError，
        绝不静默返回含假性 0.0 的分布。需要零填充数组请用 prob_or_zero()。"""
        if self._underflow:
            recs = "; ".join(
                f"{r.label}: log p={r.log_prob:.3f} (相对最大值 {r.log_gap_to_max:.1f})"
                for r in self._underflow
            )
            raise PosteriorUnderflowError(
                f"{len(self._underflow)} 个假设的后验概率在 double 中下溢，"
                f"拒绝返回会误导的线性分布；请使用 log_prob。明细: {recs}"
            )
        return [math.exp(v) for v in self.log_prob]

    def prob_or_zero(self) -> List[float]:
        """线性概率，下溢项填 0.0（仅供报表/绘图；调用方须检查 has_underflow）。"""
        return [math.exp(v) if v >= LOG_EXP_UNDERFLOW else 0.0 for v in self.log_prob]

    def report(self) -> str:
        lines = [
            f"step={self.step}  log evidence={self.log_evidence:.6f}",
            f"  下溢假设数: {len(self._underflow)}",
        ]
        for label, lp in zip(self.labels, self.log_prob):
            flag = ""
            if lp == -math.inf:
                flag = "  [似然/先验为 0，精确排除]"
            elif lp < LOG_EXP_UNDERFLOW:
                flag = "  [线性概率下溢，仅对数值有效]"
            lines.append(f"  {label}: log p = {lp:.12g}{flag}")
        return "\n".join(lines)


class BayesFilter:
    """离散假设上的在线贝叶斯滤波器，全部计算在对数域进行。"""

    def __init__(
        self,
        prior: Optional[Sequence[float]] = None,
        labels: Optional[Sequence[str]] = None,
        log_prior: Optional[Sequence[float]] = None,
    ) -> None:
        if (prior is None) == (log_prior is None):
            raise ValueError("prior 与 log_prior 必须且只能提供一个")
        if log_prior is not None:
            logs = [float(v) for v in log_prior]
            if not logs:
                raise DegeneratePriorError("空先验")
            _validate_loglik(logs)
            log_z = logsumexp(logs)
            if log_z == -math.inf:
                raise DegeneratePriorError("对数先验全为 -inf，总质量为零")
            self._log_state = [v - log_z for v in logs]
        else:
            probs = [float(v) for v in prior]  # type: ignore[arg-type]
            if not probs:
                raise DegeneratePriorError("空先验")
            for v in probs:
                if math.isnan(v) or v < 0.0 or v == math.inf:
                    raise DegeneratePriorError(f"非法先验值 {v!r}")
            total = math.fsum(probs)
            if total <= 0.0:
                raise DegeneratePriorError("先验全为零，总质量为零")
            # 不归一化到 1 再取 log，而是 log(p) - log(sum)，少一次舍入
            log_total = math.log(total)
            self._log_state = [
                math.log(v) - log_total if v > 0.0 else -math.inf for v in probs
            ]
        n = len(self._log_state)
        if labels is None:
            self.labels = tuple(f"h{i}" for i in range(n))
        else:
            if len(labels) != n:
                raise ValueError("labels 数量与假设数不一致")
            self.labels = tuple(str(x) for x in labels)
        self._log_evidence = 0.0
        self._step = 0
        self.stability_log: List[StabilityWarningRecord] = []
        self.underflow_log: List[PosteriorUnderflowRecord] = []

    # ---- 更新 ----------------------------------------------------------

    def observe(self, likelihood: Sequence[float]) -> float:
        """用线性似然 p(x|h_i) 更新。返回本次观测的对数证据 log p(x)。

        似然为 0 的项 -> log 0 = -inf（精确排除）。其余项取 log 后进入对数域，
        即使似然小到 5e-324（次正规数）也只是 log 值约 -744，绝不丢失。
        """
        logs = []
        for v in likelihood:
            v = float(v)
            if math.isnan(v) or v < 0.0 or v == math.inf:
                raise ValueError(f"非法似然 {v!r}：允许 [0, +inf) 内有限值")
            logs.append(math.log(v) if v > 0.0 else -math.inf)
        return self._advance(logs)

    def observe_log(self, log_likelihood: Sequence[float]) -> float:
        """用对数似然 log p(x|h_i) 更新。返回本次观测的对数证据。

        这是表达任意小似然的推荐方式：log L = -1e6 完全合法，
        而线性域里对应的 double 早已是 0.0。
        """
        logs = [float(v) for v in log_likelihood]
        _validate_loglik(logs)
        return self._advance(logs)

    def _advance(self, log_lik: List[float]) -> float:
        if len(log_lik) != len(self._log_state):
            raise ValueError(
                f"似然长度 {len(log_lik)} 与假设数 {len(self._log_state)} 不一致"
            )
        new_state = [s + ll for s, ll in zip(self._log_state, log_lik)]
        log_z = logsumexp(new_state)
        if log_z == -math.inf:
            raise ZeroEvidenceError(
                "该观测下所有假设似然均为零（log Z = -inf），后验无定义；状态已回滚"
            )
        self._log_state = new_state
        self._log_evidence += log_z
        self._step += 1
        finite = [v for v in new_state if v > -math.inf]
        span = max(finite) - min(finite) if len(finite) > 1 else 0.0
        if span > _SPAN_WARN:
            self.stability_log.append(
                StabilityWarningRecord(
                    kind="precision-loss-guard",
                    step=self._step,
                    span=span,
                    detail=(
                        f"对数状态跨度 {span:.3e} 超过 {_SPAN_WARN:.0e}，"
                        "double 加法对小权重项开始失去意义（远早于下溢）"
                    ),
                )
            )
        return log_z

    # ---- 查询 ----------------------------------------------------------

    @property
    def step(self) -> int:
        return self._step

    @property
    def log_evidence(self) -> float:
        """全部观测的总对数证据 log p(x_1..x_n)。"""
        return self._log_evidence

    def posterior(self) -> PosteriorResult:
        log_z = logsumexp(self._log_state)
        log_prob = tuple(v - log_z for v in self._log_state)
        under = tuple(
            PosteriorUnderflowRecord(
                label=self.labels[i],
                index=i,
                log_prob=lp,
                log_gap_to_max=lp - max(log_prob),
            )
            for i, lp in enumerate(log_prob)
            if -math.inf < lp < LOG_EXP_UNDERFLOW
        )
        self.underflow_log.extend(under)
        return PosteriorResult(
            labels=self.labels,
            log_prob=log_prob,
            log_evidence=self._log_evidence,
            step=self._step,
            _underflow=under,
        )


def naive_linear_update(prior: Sequence[float], likelihoods: Sequence[Sequence[float]]) -> List[float]:
    """朴素的概率域连乘更新，仅用于对照演示下溢，请勿在生产中使用。"""
    post = [float(p) for p in prior]
    for lik in likelihoods:
        post = [p * float(l) for p, l in zip(post, lik)]
        z = sum(post)
        post = [p / z for p in post]
    return post
