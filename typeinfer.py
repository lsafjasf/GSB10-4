"""typeinfer — 列类型推断与留出样本校验（仅使用 Python 标准库）。

核心设计
========
1. 全量流过所有行，对每列做蓄水池抽样（Vitter Algorithm L），
   样本均匀覆盖整列，而不是只看前几行；
2. 每列样本划分为推断集（默认 80%）与留出集（默认 20%），
   类型结论在留出集上验证并报告通过率；
3. 每条结论都带推断依据：样本量、取值分布、Wilson 置信下界；
4. 非空样本不足 min_samples 时拒绝下结论，并报告实际样本量；
5. 混合类型列按确定性规则处理（数值提升 / 整体提升为文本 / 拒绝），
   并附代价说明。
"""

from __future__ import annotations

import csv
import math
import random
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

# ---------------------------------------------------------------------------
# 值分类
# ---------------------------------------------------------------------------

INT_RE = re.compile(r"[+-]?\d+")
FLOAT_RE = re.compile(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?")

BOOL_TOKENS = frozenset({"true", "false", "yes", "no"})

# 默认空值标记（小写比较）。注意 "0"/"1" 视为 int，不视为 bool。
DEFAULT_NULL_TOKENS = frozenset({"", "na", "n/a", "null", "none", "nan"})

TYPE_NULL = "null"
TYPE_BOOL = "bool"
TYPE_INT = "int"
TYPE_FLOAT = "float"
TYPE_TEXT = "text"

# 状态
STATUS_INFERRED = "inferred"
STATUS_INSUFFICIENT = "insufficient_sample"
STATUS_LOW_CONFIDENCE = "low_confidence"
STATUS_EMPTY = "empty"
STATUS_REJECTED = "rejected"

# 混合类型确定性处理
DECISION_PROMOTE = "promote"            # 数值族内提升（int/float/bool -> 公共超类型）
DECISION_PROMOTE_TEXT = "promote_to_text"  # 全列无损提升为文本
DECISION_REJECT = "reject"              # 拒绝下结论


def classify(value: str, null_tokens=DEFAULT_NULL_TOKENS) -> str:
    """把单个字符串值分类为 null/bool/int/float/text。"""
    s = value.strip()
    if s.lower() in null_tokens:
        return TYPE_NULL
    if INT_RE.fullmatch(s):
        return TYPE_INT
    if FLOAT_RE.fullmatch(s):
        return TYPE_FLOAT
    if s.lower() in BOOL_TOKENS:
        return TYPE_BOOL
    return TYPE_TEXT


def wilson_lower_bound(k: int, n: int, z: float = 1.96) -> float:
    """二项比例的 Wilson 95% 置信下界，作为“结论类型代表性”的置信程度。"""
    if n <= 0:
        return 0.0
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return (center - margin) / denom


# ---------------------------------------------------------------------------
# 推断结果
# ---------------------------------------------------------------------------

@dataclass
class InferenceResult:
    column: str
    status: str                          # inferred / insufficient_sample / low_confidence / empty / rejected
    type: Optional[str]                  # 结论类型；拒绝/样本不足/空列时为 None
    decision: Optional[str]              # 混合列的确定性处理：promote / promote_to_text / reject / None
    rows_seen: int                       # 全列总行数
    sample_size: int                     # 推断集中非空样本数（下结论的依据样本量）
    null_rate: float                     # 空值率（基于全部抽样估计）
    distribution: Dict[str, int]         # 推断集取值分布 {类型: 次数}
    confidence: Optional[float]          # 结论类型覆盖率的 Wilson 95% 置信下界
    holdout_size: int                    # 留出集非空样本数
    holdout_pass_rate: Optional[float]   # 留出集按结论类型解析的通过率
    cost: Optional[str]                  # 混合处理的代价说明
    message: str                         # 人类可读的结论/拒绝原因

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# ---------------------------------------------------------------------------
# 推断器
# ---------------------------------------------------------------------------

class TypeInferencer:
    """流式列类型推断器。

    用法：逐行调用 add_row(values)，最后调用 infer() 得到每列 InferenceResult。
    内存占用与行数无关：每列只保留 sample_size 个蓄水池样本。
    """

    def __init__(
        self,
        n_columns: int,
        column_names: Optional[Sequence[str]] = None,
        *,
        sample_size: int = 512,
        holdout_fraction: float = 0.2,
        min_samples: int = 30,
        min_confidence: float = 0.8,
        text_tolerance: float = 0.02,
        seed: int = 42,
        null_tokens=DEFAULT_NULL_TOKENS,
    ):
        if n_columns <= 0:
            raise ValueError("n_columns 必须为正")
        if sample_size < 2:
            raise ValueError("sample_size 至少为 2")
        if not (0.0 < holdout_fraction <= 0.5):
            raise ValueError("holdout_fraction 应在 (0, 0.5] 区间")
        self.n_columns = n_columns
        self.column_names = list(column_names) if column_names else [f"col_{i}" for i in range(n_columns)]
        if len(self.column_names) != n_columns:
            raise ValueError("column_names 长度与 n_columns 不一致")
        self.sample_size = sample_size
        self.holdout_fraction = holdout_fraction
        self.min_samples = min_samples
        self.min_confidence = min_confidence
        self.text_tolerance = text_tolerance
        self.null_tokens = frozenset(t.lower() for t in null_tokens)

        self._rng = random.Random(seed)
        self._rows_seen = 0
        self._reservoirs: List[List[str]] = [[] for _ in range(n_columns)]
        # Algorithm L 的惰性调度表：行号 -> [(列, 槽位)]，任一时刻每列至多一个待采样行号
        self._schedule: Dict[int, List[tuple]] = {}
        self._w = [1.0] * n_columns       # Algorithm L 的步长因子
        self._scheduled = False           # 是否已在第 sample_size 行初始化调度

    # -- 抽样 ---------------------------------------------------------------

    def _init_schedule(self) -> None:
        s = self.sample_size
        for c in range(self.n_columns):
            self._w[c] = math.exp(math.log(self._rng.random()) / s)
            self._schedule_next(c, s - 1)
        self._scheduled = True

    def _schedule_next(self, c: int, from_index: int) -> None:
        s = self.sample_size
        denom = math.log(1.0 - self._w[c])
        if denom == 0.0:  # 数值保护：w 极度接近 1 时直接跳到很远
            return
        skip = int(math.log(self._rng.random()) / denom) + 1
        nxt = from_index + skip
        slot = self._rng.randrange(s)
        self._schedule.setdefault(nxt, []).append((c, slot))
        self._w[c] *= math.exp(math.log(self._rng.random()) / s)

    def add_row(self, values: Sequence[str]) -> None:
        """喂入一行（字符串序列）。短行缺失的列按空值处理。"""
        r = self._rows_seen
        self._rows_seen += 1
        s = self.sample_size
        if r < s:
            # 前 s 行：所有列直接入蓄水池
            for c in range(self.n_columns):
                self._reservoirs[c].append(values[c] if c < len(values) else "")
            return
        if not self._scheduled:
            self._init_schedule()
        cols = self._schedule.pop(r, None)
        if cols is None:
            return
        for c, slot in cols:
            self._reservoirs[c][slot] = values[c] if c < len(values) else ""
            self._schedule_next(c, r)

    # -- 推断 ---------------------------------------------------------------

    def infer(self) -> List[InferenceResult]:
        return [self._infer_column(c) for c in range(self.n_columns)]

    def _split_sample(self, sample: List[str]):
        idx = list(range(len(sample)))
        self._rng.shuffle(idx)
        n_holdout = int(len(sample) * self.holdout_fraction) if len(sample) >= 5 else 0
        holdout_idx = set(idx[:n_holdout])
        inference, holdout = [], []
        for i, v in enumerate(sample):
            (holdout if i in holdout_idx else inference).append(v)
        return inference, holdout

    def _infer_column(self, c: int) -> InferenceResult:
        name = self.column_names[c]
        sample = self._reservoirs[c]
        n_sampled = len(sample)
        nulls = sum(1 for v in sample if v.strip().lower() in self.null_tokens)
        null_rate = (nulls / n_sampled) if n_sampled else 0.0

        inference, holdout = self._split_sample(sample)
        dist: Dict[str, int] = {}
        for v in inference:
            t = classify(v, self.null_tokens)
            if t != TYPE_NULL:
                dist[t] = dist.get(t, 0) + 1
        n = sum(dist.values())

        holdout_nonnull = [v for v in holdout if classify(v, self.null_tokens) != TYPE_NULL]

        def result(status, type_, decision, confidence, pass_rate, cost, message):
            return InferenceResult(
                column=name, status=status, type=type_, decision=decision,
                rows_seen=self._rows_seen, sample_size=n, null_rate=round(null_rate, 6),
                distribution=dist, confidence=confidence,
                holdout_size=len(holdout_nonnull), holdout_pass_rate=pass_rate,
                cost=cost, message=message,
            )

        # 1) 整列为空
        if n == 0:
            return result(STATUS_EMPTY, None, None, None, None, None,
                          f"整列为空：共 {self._rows_seen} 行，抽样 {n_sampled} 条全部为空值，"
                          f"无法推断类型；建议人工指定类型或等出现数据后再推断。")

        # 2) 样本不足：拒绝下结论并报出样本量
        if n < self.min_samples:
            return result(STATUS_INSUFFICIENT, None, None, None, None, None,
                          f"样本不足，拒绝下结论：非空样本 {n} 条 < 最小要求 {self.min_samples} 条"
                          f"（总行数 {self._rows_seen}，抽样 {n_sampled} 条，空值率 {null_rate:.1%}）。")

        # 3) 类型归并与混合处理（确定性规则）
        type_, decision, cost, covered = self._resolve_types(dist, n)
        if type_ is None:
            # 严重混合：拒绝
            p_text = dist.get(TYPE_TEXT, 0) / n
            p_text_low = wilson_lower_bound(dist.get(TYPE_TEXT, 0), n)
            return result(STATUS_REJECTED, None, DECISION_REJECT, None, None, cost,
                          f"混合严重，拒绝下结论：文本占比 {p_text:.1%}（Wilson 下界 {p_text_low:.1%}）"
                          f"显著超过容忍度 "
                          f"{self.text_tolerance:.1%}，分布 {dist}。" + cost)

        # 4) 置信度门槛：结论类型对样本的覆盖率的 Wilson 下界
        confidence = wilson_lower_bound(covered, n)
        if confidence < self.min_confidence:
            return result(STATUS_LOW_CONFIDENCE, type_, decision, round(confidence, 4), None, cost,
                          f"置信度不足，拒绝下结论：Wilson 95% 下界 {confidence:.3f} < "
                          f"{self.min_confidence}（分布 {dist}）。")

        # 5) 留出集验证
        parser = _parser_for(type_)
        passed = sum(1 for v in holdout_nonnull if parser(v))
        pass_rate = (passed / len(holdout_nonnull)) if holdout_nonnull else None

        msg = (f"推断为 {type_}：非空样本 {n} 条，分布 {dist}，"
               f"置信度(Wilson 下界) {confidence:.3f}，"
               f"留出验证 {passed}/{len(holdout_nonnull)} 通过。")
        if decision:
            msg += f" 混合处理：{decision}。"
        return result(STATUS_INFERRED, type_, decision, round(confidence, 4),
                      round(pass_rate, 4) if pass_rate is not None else None, cost, msg)

    def _resolve_types(self, dist: Dict[str, int], n: int):
        """混合类型的确定性归并。返回 (type, decision, cost, covered_count)。"""
        types = set(dist)
        p_text = dist.get(TYPE_TEXT, 0) / n

        # 单一类型
        if len(types) == 1:
            t = next(iter(types))
            return t, None, None, dist[t]

        # 纯数值/布尔族内混合 -> 提升
        if types <= {TYPE_INT, TYPE_FLOAT, TYPE_BOOL}:
            if TYPE_FLOAT in types:
                return (TYPE_FLOAT, DECISION_PROMOTE,
                        "int/bool 提升为 float：对 2^53 以内的整数无损，超大整数会丢精度；"
                        "内存约 8 字节/值；布尔失去真假语义。",
                        sum(dist[t] for t in (TYPE_INT, TYPE_FLOAT, TYPE_BOOL) if t in dist))
            # bool + int
            return (TYPE_INT, DECISION_PROMOTE,
                    "bool 提升为 int（true->1, false->0）：数值无损，但丢失布尔语义。",
                    dist.get(TYPE_INT, 0) + dist.get(TYPE_BOOL, 0))

        # 涉及文本
        text_count = dist.get(TYPE_TEXT, 0)
        p_text_low = wilson_lower_bound(text_count, n)
        if p_text >= 0.5:
            # 文本占主导：自然文本列，少量数值按文本处理
            return (TYPE_TEXT, None,
                    f"文本占 {p_text:.1%} 为主导，按文本列处理；其中 {1 - p_text:.1%} 的数值/布尔"
                    f"值将以字符串形式保留（失去数值语义）。",
                    n)
        if p_text_low <= self.text_tolerance:
            # 仅当“文本占比显著超过容忍度”时才拒绝，避免抽样噪声造成误判
            return (TYPE_TEXT, DECISION_PROMOTE_TEXT,
                    f"全列提升为文本：解析无损（任何值都是合法字符串），代价是失去数值/布尔运算"
                    f"与排序语义（变为字典序），存储约为数值列 3-5 倍；若有损可接受，备选方案是"
                    f"把占比 {p_text:.1%} 的文本值置空（丢失这部分数据）。",
                    n)  # 文本覆盖一切
        # 中间地带：拒绝，并给出两个确定性备选及代价
        cost = (f" 确定处理：拒绝。备选 A——拆分为多列（按类型拆成 "
                f"{sorted(types)} 子列，代价：schema 膨胀、每列空值率上升、下游需多路合并）；"
                f"备选 B——整体提升为文本（代价：失去全部类型语义，数值比较/排序失效）。")
        return None, DECISION_REJECT, cost, 0


def _parser_for(type_: str):
    if type_ == TYPE_INT:
        return lambda s: INT_RE.fullmatch(s.strip()) is not None
    if type_ == TYPE_FLOAT:
        return lambda s: FLOAT_RE.fullmatch(s.strip()) is not None
    if type_ == TYPE_BOOL:
        return lambda s: s.strip().lower() in BOOL_TOKENS
    if type_ == TYPE_TEXT:
        return lambda s: True
    raise ValueError(f"未知类型: {type_}")


# ---------------------------------------------------------------------------
# CSV 便捷入口
# ---------------------------------------------------------------------------

def infer_csv(source, *, has_header: bool = True, encoding: str = "utf-8", **kwargs) -> List[InferenceResult]:
    """对 CSV 文件（路径或文件对象）做类型推断。"""
    close = False
    if isinstance(source, str):
        f = open(source, newline="", encoding=encoding)
        close = True
    else:
        f = source
    try:
        reader = csv.reader(f)
        first = next(reader, None)
        if first is None:
            return []
        names = first if has_header else [f"col_{i}" for i in range(len(first))]
        inf = TypeInferencer(len(first), names, **kwargs)
        if not has_header:
            inf.add_row(first)
        for row in reader:
            inf.add_row(row)
        return inf.infer()
    finally:
        if close:
            f.close()
