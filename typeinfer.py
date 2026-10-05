"""typeinfer — 基于统计样本的列类型推断库（仅使用 Python 标准库）。

设计原则：
1. 不只看前几行：对整列做均匀随机抽样（CSV 走水库抽样，内存中数据直接全量/抽样）。
2. 样本不足拒绝下结论：训练样本数 < min_samples 时返回 status='insufficient_sample'。
3. 置信度可量化：用 Wilson 分数区间下界作为"纯度置信度"，而不是简单比例。
4. 留出验证：样本切分为训练/留出两段，推断结果在留出集上回测，给出通过率。
5. 混合类型给出确定处理：promote（提升）/ split（拆分）/ reject（拒绝），并说明代价。
"""

from __future__ import annotations

import csv
import math
import random
import re
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Iterator, Mapping, Optional

# ---------------------------------------------------------------- 标量识别

NULL_TOKENS = frozenset({"", "null", "none", "na", "n/a", "nan", "-", "--"})

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)[eE][+-]?\d+$|^[+-]?(\d+\.\d*|\.\d+)$")
_BOOL_TOKENS = {"true": True, "false": False, "yes": True, "no": False}
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)
# 日期预判：不像日期的值直接跳过 strptime（对纯文本列提速约一个数量级）
_DATE_HINT = re.compile(r"^\d{4}[-/]?\d{2}[-/]?\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")

SCALAR_TYPES = ("bool", "int", "float", "date", "string")


def is_null(value) -> bool:
    return value is None or str(value).strip().lower() in NULL_TOKENS


def detect_scalar(value) -> str:
    """识别单个非空值的标量类型。顺序：bool -> int -> float -> date -> string。"""
    text = str(value).strip()
    low = text.lower()
    if low in _BOOL_TOKENS:
        return "bool"
    if _INT_RE.match(text):
        return "int"
    if _FLOAT_RE.match(text):
        return "float"
    if _DATE_HINT.match(text):
        for fmt in _DATE_FORMATS:
            try:
                datetime.strptime(text, fmt)
                return "date"
            except ValueError:
                pass
    return "string"


def parse_value(value, col_type: str):
    """按推断类型解析值；无法解析抛 ValueError。空值一律解析为 None。"""
    if is_null(value):
        return None
    text = str(value).strip()
    if col_type == "int":
        return int(text)
    if col_type == "float":
        return float(text)
    if col_type == "bool":
        return _BOOL_TOKENS[text.lower()]
    if col_type == "date":
        if _DATE_HINT.match(text):
            for fmt in _DATE_FORMATS:
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    continue
        raise ValueError(f"无法解析为日期: {value!r}")
    if col_type == "string":
        return text
    raise ValueError(f"未知类型: {col_type!r}")


def _parses(value, col_type: str) -> bool:
    try:
        parse_value(value, col_type)
        return True
    except (ValueError, KeyError):
        return False


# ---------------------------------------------------------------- 统计工具

def wilson_lower_bound(k: int, n: int, z: float = 1.96) -> float:
    """二项比例 p=k/n 的 Wilson 分数区间下界（置信度由 z 控制，默认 95%）。

    相比朴素比例 k/n，小样本下会自动收紧，避免"3/3 就敢下结论"。
    """
    if n <= 0:
        return 0.0
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return (center - margin) / denom


def _column_seed(seed: int, name: str) -> int:
    # 不用内置 hash()（每次进程随机），保证跨运行可复现。
    return zlib.crc32(f"{seed}:{name}".encode("utf-8"))


# ---------------------------------------------------------------- 结果结构

@dataclass
class InferenceResult:
    column: str
    type: Optional[str]          # 推断类型；无法下结论时为 None；全空列为 'empty'
    status: str                  # ok / insufficient_sample / empty / mixed / validation_failed
    confidence: Optional[float]  # 纯度的 Wilson 下界（0~1）
    purity: Optional[float]      # 训练集中主导类型的朴素占比
    sample_size: int             # 实际用于推断的训练样本量（非空值）
    required_samples: int        # 下结论所需的最小样本量
    total_rows: int              # 输入总行数
    null_count: int              # 空值个数
    type_counts: dict = field(default_factory=dict)   # 训练集取值分布
    decision: Optional[dict] = None                   # 混合类型的处理决定与代价
    holdout_size: int = 0
    holdout_pass_rate: Optional[float] = None         # 留出集验证通过率

    def explain(self) -> str:
        lines = [
            f"列 {self.column!r}: 状态={self.status}, 类型={self.type}",
            f"  样本: 总行数={self.total_rows}, 非空={self.total_rows - self.null_count}, "
            f"训练样本={self.sample_size} (要求>={self.required_samples}), 留出={self.holdout_size}",
        ]
        if self.type_counts:
            dist = ", ".join(f"{t}:{c}" for t, c in sorted(self.type_counts.items(), key=lambda x: -x[1]))
            lines.append(f"  取值分布: {dist}")
        if self.purity is not None:
            lines.append(f"  纯度={self.purity:.4f}, 置信度(Wilson下界,95%)={self.confidence:.4f}")
        if self.decision:
            lines.append(f"  混合处理: {self.decision['action']} -> {self.decision.get('detail','')}"
                         f"；代价: {self.decision['cost']}")
        if self.holdout_pass_rate is not None:
            lines.append(f"  留出验证通过率={self.holdout_pass_rate:.4f} ({self.holdout_size} 条)")
        return "\n".join(lines)


# ---------------------------------------------------------------- 核心推断

def _promotion_target(observed: set) -> str:
    if observed <= {"int", "float"}:
        return "float"
    if observed <= {"int", "float", "date"} and "string" not in observed:
        # 日期与数字混合无法安全提升为单一数值/日期类型
        return "string"
    return "string"


_PROMOTE_COST = {
    "float": "整数被提升为浮点：超出 2^53 的整数会丢精度；下游失去整数语义",
    "string": "全部按文本处理：失去数值/日期运算与排序语义，存储变大",
}


def infer_column(
    values: Iterable,
    name: str = "",
    *,
    min_samples: int = 30,
    purity_threshold: float = 0.95,
    z: float = 1.96,
    holdout_ratio: float = 0.25,
    validation_threshold: float = 0.95,
    mixed_policy: str = "reject",   # reject / promote / split
    seed: int = 0,
) -> InferenceResult:
    """对一列样本做类型推断。

    流程：去空值 -> 确定性洗牌 -> 切训练/留出 -> 训练集统计取值分布
    -> Wilson 下界 >= purity_threshold 才下结论 -> 留出集回测。
    """
    if mixed_policy not in ("reject", "promote", "split"):
        raise ValueError(f"未知 mixed_policy: {mixed_policy!r}")

    values = [str(v) for v in values]
    total = len(values)
    non_null = [v for v in values if not is_null(v)]
    null_count = total - len(non_null)

    if not non_null:
        return InferenceResult(
            column=name, type="empty", status="empty", confidence=None, purity=None,
            sample_size=0, required_samples=min_samples, total_rows=total,
            null_count=null_count,
        )

    rng = random.Random(_column_seed(seed, name))
    shuffled = list(non_null)
    rng.shuffle(shuffled)
    n_holdout = int(len(shuffled) * holdout_ratio) if len(shuffled) >= 8 else 0
    holdout, train = shuffled[:n_holdout], shuffled[n_holdout:]

    if len(train) < min_samples:
        return InferenceResult(
            column=name, type=None, status="insufficient_sample",
            confidence=None, purity=None, sample_size=len(train),
            required_samples=min_samples, total_rows=total, null_count=null_count,
            holdout_size=len(holdout),
        )

    counts = Counter(detect_scalar(v) for v in train)
    dominant, dom_count = counts.most_common(1)[0]
    purity = dom_count / len(train)
    confidence = wilson_lower_bound(dom_count, len(train), z)

    # 只观察到单一类型、但置信度仍达不到阈值：这是"样本不足"，
    # 而不是"混合类型"——没有任何证据说明存在第二种类型。
    if len(counts) == 1 and confidence < purity_threshold:
        return InferenceResult(
            column=name, type=None, status="insufficient_sample",
            confidence=confidence, purity=purity, sample_size=len(train),
            required_samples=min_samples, total_rows=total, null_count=null_count,
            type_counts=dict(counts), holdout_size=len(holdout),
        )

    base = dict(
        column=name, confidence=confidence, purity=purity, sample_size=len(train),
        required_samples=min_samples, total_rows=total, null_count=null_count,
        type_counts=dict(counts), holdout_size=len(holdout),
    )

    if confidence >= purity_threshold:
        candidate, status, decision = dominant, "ok", None
    else:
        # 混合类型：按策略给出确定处理
        if mixed_policy == "reject":
            return InferenceResult(
                type=None, status="mixed",
                decision={
                    "action": "reject",
                    "detail": f"取值分布 {dict(counts)}，纯度置信度 {confidence:.4f} < {purity_threshold}",
                    "cost": "整列被拒绝，需人工指定类型或清洗后重跑；不会静默猜错",
                },
                **base,
            )
        if mixed_policy == "promote":
            target = _promotion_target(set(counts))
            candidate, status = target, "ok"
            decision = {
                "action": "promote",
                "detail": f"{dict(counts.most_common())} 提升为 {target}",
                "cost": _PROMOTE_COST[target],
            }
        else:  # split
            minorities = {t: c for t, c in counts.items() if t != dominant}
            minority_n = sum(minorities.values())
            candidate, status = dominant, "ok"
            decision = {
                "action": "split",
                "detail": f"主类型 {dominant}；少数派 {minorities} 需拆到附属列或剔除",
                "cost": f"约 {minority_n}/{len(train)} "
                        f"({minority_n / len(train):.2%}) 的记录不属于主类型，"
                        f"拆分增加列数/下游 join 成本，剔除则丢数据",
            }

    holdout_pass_rate = None
    if holdout:
        ok = sum(1 for v in holdout if _parses(v, candidate))
        holdout_pass_rate = ok / len(holdout)
        if holdout_pass_rate < validation_threshold:
            status = "validation_failed"

    return InferenceResult(
        type=candidate, status=status, decision=decision,
        holdout_pass_rate=holdout_pass_rate, **base,
    )


def infer_columns(columns: Mapping[str, Iterable], **kwargs) -> dict:
    """对 {列名: 值序列} 批量推断，返回 {列名: InferenceResult}。"""
    return {name: infer_column(vals, name, **kwargs) for name, vals in columns.items()}


# ---------------------------------------------------------------- CSV 入口

def infer_csv(
    path: str,
    *,
    sample_rows: int = 2000,
    seed: int = 42,
    encoding: str = "utf-8",
    **kwargs,
) -> dict:
    """流式读取 CSV，用水库抽样抽取 sample_rows 行后逐列推断。

    水库抽样保证每一行被抽中的概率相同 —— 不会只看文件前几行；
    内存占用 O(sample_rows * 列数)，与文件大小无关。
    """
    rng = random.Random(seed)
    sampled: list = []
    seen = 0
    with open(path, newline="", encoding=encoding) as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return {}
        for row in reader:
            seen += 1
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            if len(sampled) < sample_rows:
                sampled.append(row)
            else:
                j = rng.randrange(seen)
                if j < sample_rows:
                    sampled[j] = row

    columns = {name: [] for name in header}
    for row in sampled:
        for i, name in enumerate(header):
            columns[name].append(row[i] if i < len(row) else "")
    results = infer_columns(columns, **kwargs)
    for res in results.values():
        res.total_rows = seen  # 报告真实总行数而非样本行数
    return results
