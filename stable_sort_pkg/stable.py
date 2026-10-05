"""稳定的多级排序实现（Python 3 标准库）。

平局规则（确定性总序）
---------------------
每条记录的排序键由 L 级业务键 + 1 个隐式追加键组成：

    (level_1, level_2, ..., level_L, original_index)

1. 逐级比较：某一级不相等时由该级决定先后，后面的级不再参与。
2. 所有业务键都相等（完全平局）时，比较 original_index，即记录进入
   stable_sort 时在输入列表中的下标（0 起始）。因此等价元素严格保持
   输入时的相对顺序 —— 这是确定性的：相同的输入序列重复排序，输出
   逐位相同；即使输入被预先整体打乱，"相等元素之间的相对次序"这个
   语义也由它们在当次输入中的位置唯一确定。

单级内部的桶顺序（与升/降序无关，只由 nulls 参数决定）
------------------------------------------------------
每个单级项 _Term 有三个桶：

    0  可比值（present）
    1  空值：键缺失 / None / 抛异常的取值函数（missing）
    2  不可比值：比较时抛 TypeError，或 float NaN（incomparable）

- nulls='last'（默认）：桶顺序 0 < 1 < 2
- nulls='first'：       桶顺序 1 < 0 < 2
  （不可比值永远落在最后，避免它污染正常值的顺序）

桶内规则：

- present 桶：按值自然序比较；descending=True 时反转。
  跨类型不可比时（如 int 与 str）回退到全序键
      (类型所在模块名, 类型限定名, repr(值))
  由于全序键以类型名为前缀，这与"同类型按值、跨类型按类型名"完全
  一致，不会破坏传递性。descending 反转的是该级"完整全序"——
  包括回退序，因此降序时跨类型顺序也整体翻转。
- incomparable 桶：直接按上述全序键比较（descending 同样反转）。
- missing 桶：桶内一律视为相等，交给下一级裁决。

回退是确定性的：同样的数据跑任意次、在任意机器上，桶序与全序键都
不变。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Optional, Sequence, Union

__all__ = [
    "SortKey",
    "MISSING",
    "stable_sort",
    "multipass_sort",
    "explain_vs_primary",
]


class _Missing:
    """键缺失的内部哨兵。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<MISSING>"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


@dataclass(frozen=True)
class SortKey:
    """一级排序键。

    field:      记录字段名（对 dict 记录）或取值函数 (record)->value。
    descending: True 降序；默认升序。只影响可比值桶内顺序。
    nulls:      'last'（默认）或 'first'，空值/缺失值的放置位置，
                与升降序无关（SQL 语义 NULLS LAST / NULLS FIRST）。
    """

    field: Union[str, Callable[[Any], Any]]
    descending: bool = False
    nulls: str = "last"

    def __post_init__(self) -> None:
        if self.nulls not in ("first", "last"):
            raise ValueError("nulls 只能是 'first' 或 'last'")
        if not callable(self.field) and not isinstance(self.field, str):
            raise TypeError("field 必须是字段名字符串或取值函数")

    def extract(self, record: Any) -> Any:
        """取值；缺失、None、取值异常一律归一化为 MISSING。"""
        try:
            if isinstance(self.field, str):
                if isinstance(record, dict):
                    if self.field not in record:
                        return MISSING
                    value = record[self.field]
                else:
                    value = getattr(record, self.field, MISSING)
            else:
                value = self.field(record)
        except Exception:
            return MISSING
        if value is None:
            return MISSING
        return value


def _fallback_key(value: Any) -> tuple:
    """跨类型/不可比值的确定性全序键。"""
    cls = type(value)
    module = getattr(cls, "__module__", "") or ""
    qualname = getattr(cls, "__qualname__", cls.__name__)
    try:
        rendered = repr(value)
    except Exception:
        rendered = f"<unreprable {id(value)}>"
    return (module, qualname, rendered)


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


# 类别（语义）与桶号（位置）分离：nulls='first' 时缺失会排到前面，
# 但它"桶内相等"的语义不能丢，所以 _Term 同时携带二者。
_CAT_PRESENT = 0
_CAT_MISSING = 1
_CAT_INCOMPARABLE = 2


class _Term(tuple):
    """单级比较项：(bucket, category, payload, descending)。

    bucket 只决定桶间先后；category 决定桶内怎么比。payload 为
      - present 桶：原始值
      - missing 桶：None
      - incomparable 桶：该值的全序键（tuple）
    """

    __slots__ = ()

    def __new__(cls, bucket: int, category: int, payload: Any, descending: bool):
        return tuple.__new__(cls, (bucket, category, payload, descending))

    @staticmethod
    def _present_relation(payload_a: Any, payload_b: Any) -> int:
        """present 桶内两值的严格三态：-1 小于 / 0 相等 / 1 大于。"""
        try:
            if payload_a < payload_b:
                return -1
            if payload_b < payload_a:
                return 1
            return 0
        except TypeError:
            # 跨类型不可比：回退到 (模块名, 类型限定名, repr) 全序键。
            fa = _fallback_key(payload_a)
            fb = _fallback_key(payload_b)
            if fa < fb:
                return -1
            if fb < fa:
                return 1
            return 0

    def __lt__(self, other: "_Term") -> bool:  # type: ignore[override]
        bucket_a, cat_a, payload_a, desc_a = self
        bucket_b, cat_b, payload_b, _ = other
        if bucket_a != bucket_b:
            return bucket_a < bucket_b
        if cat_a == _CAT_MISSING:
            return False  # 缺失桶桶内相等，交由后续级裁决
        if cat_a == _CAT_INCOMPARABLE:
            relation = -1 if payload_a < payload_b else (
                1 if payload_b < payload_a else 0
            )
        else:
            relation = self._present_relation(payload_a, payload_b)
        if relation == 0:
            return False
        return (relation > 0) if desc_a else (relation < 0)

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, _Term):
            return NotImplemented
        bucket_a, cat_a, payload_a, _ = self
        bucket_b, cat_b, payload_b, _ = other
        if bucket_a != bucket_b or cat_a != cat_b:
            return False
        if cat_a == _CAT_MISSING:
            return True
        if cat_a == _CAT_INCOMPARABLE:
            return payload_a == payload_b
        return self._present_relation(payload_a, payload_b) == 0

    def __hash__(self) -> int:  # type: ignore[override]
        return id(self)


def _make_term(key: SortKey, value: Any) -> _Term:
    if value is MISSING:
        category = _CAT_MISSING
        payload: Any = None
    elif _is_nan(value):
        category = _CAT_INCOMPARABLE
        payload = _fallback_key(value)
    else:
        category = _CAT_PRESENT
        payload = value
    if key.nulls == "last":
        # present 0，missing 1，incomparable 2（不可比永远最后）。
        bucket = category
    else:
        # nulls='first'：missing 0，present 1，incomparable 2。
        bucket = {_CAT_MISSING: 0, _CAT_PRESENT: 1,
                  _CAT_INCOMPARABLE: 2}[category]
    return _Term(bucket, category, payload, key.descending)


def _build_sort_key(keys: Sequence[SortKey], index: int, record: Any) -> tuple:
    terms: List[_Term] = [_make_term(k, k.extract(record)) for k in keys]
    terms.append(index)  # 隐式末级：原始下标，保证稳定与确定性
    return tuple(terms)


def stable_sort(
    records: Iterable[Any],
    keys: Sequence[SortKey],
    in_place: bool = False,
) -> List[Any]:
    """按多级键做一次稳定排序，返回新列表（in_place=True 时原地排并返回）。

    排序键末尾隐式追加原始下标，因此是严格全序：完全平局也有确定结果，
    同批数据重复排序逐位一致。
    """
    if not keys:
        raise ValueError("至少需要一级 SortKey")
    ordered = records if in_place else list(records)
    if in_place and not isinstance(ordered, list):
        raise TypeError("in_place=True 要求 records 本身是 list")
    decorated = [
        (_build_sort_key(keys, index, record), record)
        for index, record in enumerate(ordered)
    ]
    decorated.sort(key=lambda pair: pair[0])
    result = [pair[1] for pair in decorated]
    if in_place:
        ordered[:] = result
        return ordered
    return result


def multipass_sort(records: Iterable[Any], keys: Sequence[SortKey]) -> List[Any]:
    """对拍实现：从最低优先级键开始，逐级做稳定排序（radix 式）。

    每一级都用 Python 内置稳定排序，只带该级键；利用稳定排序的性质，
    逐级排序后的最终次序与一次成型的 stable_sort 必须完全一致。
    """
    if not keys:
        raise ValueError("至少需要一级 SortKey")
    result = list(records)
    for index_pos in range(len(keys) - 1, -1, -1):
        key = keys[index_pos]
        result.sort(key=lambda record, k=key: _make_term(k, k.extract(record)))
    return result


def explain_vs_primary(
    records: Sequence[Any],
    keys: Sequence[SortKey],
) -> dict:
    """比较"多级排序结果"与"只按主键排序（平局保原始序）"的差异。

    返回报告 dict，说明二者如何互相解释：
      - 主键值互异的记录，两种排法位置完全相同（次级键无从发挥）；
      - 位置只会在"主键相等的等值组"内部发生变化，且组内次序由
        第 2..L 级键决定，仍然平局时由原始下标决定。
    """
    multi = stable_sort(records, keys)
    primary = stable_sort(records, keys[:1])
    multi_position = {id(record): i for i, record in enumerate(multi)}
    primary_position = {id(record): i for i, record in enumerate(primary)}

    moved = 0
    group_sizes: dict = {}
    group_examples: List[dict] = []

    # 以主键值划分等值组（MISSING / NaN 也各自成组，分组用恒等语义）。
    groups: List[List[Any]] = []
    current: List[Any] = []
    current_key: Any = None
    for record in primary:
        value = keys[0].extract(record)
        if current and not _same_primary(current_key, value):
            groups.append(current)
            current = []
        current.append(record)
        current_key = value
    if current:
        groups.append(current)

    for group in groups:
        if len(group) <= 1:
            continue
        size = len(group)
        group_sizes[size] = group_sizes.get(size, 0) + 1
        group_ids = {id(r) for r in group}
        # 多级排序结果中该组记录占据的仍是一段连续区间。
        positions = sorted(multi_position[id(r)] for r in group)
        # 组内每个成员（按主键排序中的先后）在多级排序中的位置，
        # 直接展示"等值组内部的重排"。
        member_moves = [
            (primary_position[id(r)], multi_position[id(r)]) for r in group
        ]
        contiguous = positions == list(range(positions[0], positions[0] + size))
        reordered = [multi[pos] for pos in positions]
        for before_rec, after_rec in zip(group, reordered):
            if id(before_rec) != id(after_rec):
                moved += 1
        if len(group_examples) < 5:
            group_examples.append(
                {
                    "primary_value": _safe_repr(keys[0].extract(group[0])),
                    "size": size,
                    "contiguous_in_multi": contiguous,
                    "member_moves_primary_to_multi": member_moves,
                }
            )

    return {
        "total": len(records),
        "moved_within_primary_tie_groups": moved,
        "tie_group_count": sum(group_sizes.values()),
        "tie_group_size_distribution": dict(sorted(group_sizes.items())),
        "interpretation": (
            "只按主键排序时，主键相等的记录保持原始相对顺序；多级排序"
            "仅在这些等值组内部用后续键重排（仍平局则用原始下标）。"
            "因此差异只出现在等值组内，等值组之间的相对位置不变，"
            "每组在多级结果中仍占据连续区间。"
        ),
        "examples": group_examples,
    }


def _same_primary(a: Any, b: Any) -> bool:
    if a is MISSING or b is MISSING:
        return a is b
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
    try:
        return bool(a == b)
    except Exception:
        return _fallback_key(a) == _fallback_key(b)


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return "<unreprable>"

