"""多级稳定排序库（Python 3，仅标准库）。

平局规则（按顺序逐级比较）：
1. 第 1 级键不同 -> 由第 1 级决定（主键优先）。
2. 第 1 级相等 -> 比较第 2 级，依此类推。
3. 所有显式排序键都等价 -> 保持输入中的原始先后（稳定排序兜底）。
   实现方式：sorted 本身稳定，且每个元素的键元组末尾附带原始下标，
   因此即使键元组完全相等，顺序也由原始下标唯一确定。

缺失 / 不可比键的确定回退：
- 键不存在、值为 None、值为 NaN 一律视为“缺失”，无论升/降序恒排最后。
- 同一级键类型不可比（如 int 与 str）时，按类型秩回退：
  数字(含 float) < bool < str < 其它(按 repr 字符串)。
- 同类型内按正常值比较；全部回退比较仍等价时，再走原始下标兜底。
"""
from functools import total_ordering

MISSING = None  # None 与键缺失统一按缺失处理


@total_ordering
class _Reversed:
    """把一个可比值包成反向比较，用于某一级的降序，不影响其它级。"""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return other.value < self.value

    def __eq__(self, other):
        return self.value == other.value


def _is_missing(value):
    if value is None:
        return True
    # NaN：NaN != NaN
    return isinstance(value, float) and value != value


def _rank(value):
    """把不可比类型归一为 (类型秩, 同秩内可比值)。"""
    if isinstance(value, bool):
        return (1, value)
    if isinstance(value, (int, float)):
        return (0, float(value))
    if isinstance(value, str):
        return (2, value)
    # 其它任何类型：用 repr 兜底，保证确定且不会抛 TypeError
    return (3, repr(value))


def _parse_specs(specs):
    parsed = []
    for spec in specs:
        if isinstance(spec, str):
            parsed.append((spec, False))
        else:
            parsed.append((spec[0], bool(spec[1])))
    return parsed


def make_sort_key(specs):
    """构造排序键函数。

    specs: ["age", ("name", True)]，True 表示该级降序。
    每行返回: (键0元素, 键1元素, ..., 原始下标)
    """
    parsed = _parse_specs(specs)

    def key_func(indexed_row):
        index, row = indexed_row
        parts = []
        for name, descending in parsed:
            value = row.get(name) if isinstance(row, dict) else getattr(row, name, None)
            if _is_missing(value):
                # 缺失标记恒为 1（存在为 0），不参与降序翻转 -> 恒排最后
                parts.append((1,))
            else:
                ranked = _rank(value)
                parts.append((0, _Reversed(ranked) if descending else ranked))
        parts.append(index)  # 最终平局兜底：原始下标
        return tuple(parts)

    return key_func


def stable_sort(rows, specs):
    """对 rows（dict 序列）做多级稳定排序，返回新列表。

    同一批数据重复调用结果完全一致（纯函数，不修改输入）。
    """
    indexed = list(enumerate(rows))
    indexed.sort(key=make_sort_key(specs))
    return [row for _, row in indexed]
