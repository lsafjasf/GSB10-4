"""window_frame.py — 窗口函数帧（frame）边界计算库（仅标准库）。

语义速览（详细见 SEMANTICS.md）：
- 行按 (partition_key, order_key, 原始下标) 稳定排序，原始下标作为决胜键，
  排序键不稳定（重复）时结果仍可复现。
- 帧边界一律为**闭区间** [start, end]（分区内位置，含两端）；start > end 表示空帧。
- ROWS  : 按物理行数偏移，越界裁剪到分区内，并列值被**截断**（各行帧可不同）。
- RANGE : 按排序键的值偏移，并列行（peer）**整体并入**帧（同键各行帧完全相同）。
- GROUPS: 按 peer 组数偏移，组边界裁剪，并列行同帧。
- 空帧取值（SQL 标准）：count=0，sum/avg/min/max/first_value/last_value=None。
- 默认帧（未显式给出时）：RANGE UNBOUNDED PRECEDING AND CURRENT ROW。
"""

from array import array
from bisect import bisect_left, bisect_right
from collections import deque, namedtuple

__all__ = [
    "UNBOUNDED", "CURRENT_ROW", "PRECEDING", "FOLLOWING",
    "Frame", "DEFAULT_FRAME", "EMPTY_AGG",
    "frame_bounds", "evaluate", "evaluate_naive",
]

# ---- 边界类型常量 -----------------------------------------------------------
UNBOUNDED = "UNBOUNDED"      # UNBOUNDED PRECEDING / UNBOUNDED FOLLOWING
CURRENT_ROW = "CURRENT_ROW"  # CURRENT ROW
PRECEDING = "PRECEDING"      # <offset> PRECEDING
FOLLOWING = "FOLLOWING"      # <offset> FOLLOWING

Frame = namedtuple("Frame", ["mode", "start", "end"])
Frame.__doc__ = (
    "帧定义：mode ∈ {'ROWS','RANGE','GROUPS'}；start/end 为 (kind, offset)。\n"
    "kind ∈ {UNBOUNDED, CURRENT_ROW, PRECEDING, FOLLOWING}；offset 仅 PRECEDING/FOLLOWING 使用。\n"
    "start 的 UNBOUNDED 表示 UNBOUNDED PRECEDING，end 的 UNBOUNDED 表示 UNBOUNDED FOLLOWING；\n"
    "PRECEDING/FOLLOWING 在两侧均允许（如 BETWEEN 2 PRECEDING AND 1 PRECEDING），\n"
    "若某行 start > end 则该行为空帧。"
)

DEFAULT_FRAME = Frame("RANGE", (UNBOUNDED, None), (CURRENT_ROW, None))

# 空帧时各聚合函数的确定取值（SQL 标准）
EMPTY_AGG = {
    "count": 0,
    "sum": None,
    "avg": None,
    "min": None,
    "max": None,
    "first_value": None,
    "last_value": None,
}

_START_KINDS = (UNBOUNDED, CURRENT_ROW, PRECEDING, FOLLOWING)
_END_KINDS = (UNBOUNDED, CURRENT_ROW, PRECEDING, FOLLOWING)


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _check_frame(frame):
    if frame.mode not in ("ROWS", "RANGE", "GROUPS"):
        raise ValueError("未知帧模式: %r" % (frame.mode,))
    sk, so = frame.start
    ek, eo = frame.end
    if sk not in _START_KINDS:
        raise ValueError("非法帧起点: %r" % (sk,))
    if ek not in _END_KINDS:
        raise ValueError("非法帧终点: %r" % (ek,))
    for kind, off in ((sk, so), (ek, eo)):
        if kind in (PRECEDING, FOLLOWING):
            if not _is_number(off) or off < 0:
                raise ValueError("偏移量必须 >= 0: (%r, %r)" % (kind, off))
            if frame.mode == "GROUPS" and not (isinstance(off, int) and not isinstance(off, bool)):
                raise ValueError("GROUPS 偏移量必须是非负整数: %r" % (off,))


# ---- 帧边界计算 -------------------------------------------------------------

def _rows_bounds(n, frame):
    """ROWS 模式：返回 (starts, ends)，越界裁剪到 [0, n-1]。"""
    sk, so = frame.start
    ek, eo = frame.end
    if sk == UNBOUNDED:
        starts = [0] * n
    elif sk == CURRENT_ROW:
        starts = list(range(n))
    elif sk == PRECEDING:
        starts = [max(0, i - so) for i in range(n)]
    else:  # FOLLOWING：越过分区末尾 -> n（必为空帧）
        starts = [min(n, i + so) for i in range(n)]
    if ek == UNBOUNDED:
        ends = [n - 1] * n
    elif ek == CURRENT_ROW:
        ends = list(range(n))
    elif ek == FOLLOWING:
        ends = [min(n - 1, i + eo) for i in range(n)]
    else:  # PRECEDING：越过分区开头 -> -1（必为空帧）
        ends = [max(-1, i - eo) for i in range(n)]
    return starts, ends


def _range_bounds(keys, frame):
    """RANGE 模式：按值偏移，peer 整体并入；返回 (starts, ends)。"""
    n = len(keys)
    sk, so = frame.start
    ek, eo = frame.end
    if sk == UNBOUNDED:
        starts = [0] * n
    elif sk == CURRENT_ROW:
        starts = [bisect_left(keys, keys[i], 0, n) for i in range(n)]
    elif sk == PRECEDING:  # 第一个 >= key-offset 的位置
        starts = [bisect_left(keys, keys[i] - so, 0, n) for i in range(n)]
    else:  # FOLLOWING: 第一个 >= key+offset 的位置
        starts = [bisect_left(keys, keys[i] + so, 0, n) for i in range(n)]
    if ek == UNBOUNDED:
        ends = [n - 1] * n
    elif ek == CURRENT_ROW:
        ends = [bisect_right(keys, keys[i], 0, n) - 1 for i in range(n)]
    elif ek == FOLLOWING:  # 最后一个 <= key+offset 的位置
        ends = [bisect_right(keys, keys[i] + eo, 0, n) - 1 for i in range(n)]
    else:  # PRECEDING: 最后一个 <= key-offset 的位置
        ends = [bisect_right(keys, keys[i] - eo, 0, n) - 1 for i in range(n)]
    return starts, ends


def _groups_bounds(keys, frame):
    """GROUPS 模式：按 peer 组数偏移；返回 (starts, ends)。"""
    n = len(keys)
    # 每个位置所属的组号 + 各组的 [起始, 结束] 位置
    group_of = [0] * n
    group_start = []
    group_end = []
    i = 0
    while i < n:
        j = bisect_right(keys, keys[i], i, n)
        group_start.append(i)
        group_end.append(j - 1)
        for p in range(i, j):
            group_of[p] = len(group_start) - 1
        i = j
    g = len(group_start)
    sk, so = frame.start
    ek, eo = frame.end
    if sk == UNBOUNDED:
        starts = [0] * n
    elif sk == CURRENT_ROW:
        starts = [group_start[group_of[i]] for i in range(n)]
    elif sk == PRECEDING:
        starts = [group_start[max(0, group_of[i] - so)] for i in range(n)]
    else:  # FOLLOWING：越过最后一组 -> n（必为空帧）
        starts = [group_start[group_of[i] + so] if group_of[i] + so < g else n
                  for i in range(n)]
    if ek == UNBOUNDED:
        ends = [n - 1] * n
    elif ek == CURRENT_ROW:
        ends = [group_end[group_of[i]] for i in range(n)]
    elif ek == FOLLOWING:
        ends = [group_end[min(g - 1, group_of[i] + eo)] for i in range(n)]
    else:  # PRECEDING：越过第一组 -> -1（必为空帧）
        ends = [group_end[group_of[i] - eo] if group_of[i] - eo >= 0 else -1
                for i in range(n)]
    return starts, ends


def frame_bounds(keys, frame):
    """对一个已按排序键升序排列的分区，计算每行的帧边界（闭区间，含端点）。

    参数:
        keys  : 分区内各行排序键，必须升序。
        frame : Frame 实例。
    返回:
        (starts, ends)：两个长度为 n 的列表。starts[i] > ends[i] 表示第 i 行为空帧。
    """
    _check_frame(frame)
    n = len(keys)
    if n == 0:
        return [], []
    if frame.mode == "ROWS":
        return _rows_bounds(n, frame)
    if frame.mode == "RANGE":
        return _range_bounds(keys, frame)
    return _groups_bounds(keys, frame)


# ---- 流式帧边界（evaluate 内部使用，降低内存峰值） ----------------------------

def _rows_bounds_iter(n, frame):
    sk, so = frame.start
    ek, eo = frame.end
    for i in range(n):
        if sk == UNBOUNDED:
            s = 0
        elif sk == CURRENT_ROW:
            s = i
        elif sk == PRECEDING:
            s = i - so
            if s < 0:
                s = 0
        else:
            s = i + so if i + so < n else n
        if ek == UNBOUNDED:
            e = n - 1
        elif ek == CURRENT_ROW:
            e = i
        elif ek == FOLLOWING:
            e = i + eo
            if e > n - 1:
                e = n - 1
        else:
            e = i - eo if i - eo >= 0 else -1
        yield s, e


def _range_bounds_iter(keys, frame):
    n = len(keys)
    sk, so = frame.start
    ek, eo = frame.end
    for i in range(n):
        k = keys[i]
        if sk == UNBOUNDED:
            s = 0
        elif sk == CURRENT_ROW:
            s = bisect_left(keys, k, 0, n)
        elif sk == PRECEDING:
            s = bisect_left(keys, k - so, 0, n)
        else:
            s = bisect_left(keys, k + so, 0, n)
        if ek == UNBOUNDED:
            e = n - 1
        elif ek == CURRENT_ROW:
            e = bisect_right(keys, k, 0, n) - 1
        elif ek == FOLLOWING:
            e = bisect_right(keys, k + eo, 0, n) - 1
        else:
            e = bisect_right(keys, k - eo, 0, n) - 1
        yield s, e


def _groups_bounds_iter(keys, frame):
    # 组边界用 array('i')：100 万组时也只占约 8 MB（两组各 4 字节/组）
    n = len(keys)
    gstart = array("i")
    gend = array("i")
    i = 0
    while i < n:
        j = bisect_right(keys, keys[i], i, n)
        gstart.append(i)
        gend.append(j - 1)
        i = j
    g = len(gstart)
    sk, so = frame.start
    ek, eo = frame.end
    gi = 0
    for i in range(n):
        while i > gend[gi]:
            gi += 1
        if sk == UNBOUNDED:
            s = 0
        elif sk == CURRENT_ROW:
            s = gstart[gi]
        elif sk == PRECEDING:
            s = gstart[gi - so] if gi - so >= 0 else 0
        else:
            gs = gi + so
            s = gstart[gs] if gs < g else n
        if ek == UNBOUNDED:
            e = n - 1
        elif ek == CURRENT_ROW:
            e = gend[gi]
        elif ek == FOLLOWING:
            e = gend[gi + eo] if gi + eo < g else gend[g - 1]
        else:
            gs = gi - eo
            e = gend[gs] if gs >= 0 else -1
        yield s, e


def _bounds_iter(keys, frame):
    n = len(keys)
    if n == 0:
        return
    mode = frame.mode
    if mode == "ROWS":
        yield from _rows_bounds_iter(n, frame)
    elif mode == "RANGE":
        yield from _range_bounds_iter(keys, frame)
    else:
        yield from _groups_bounds_iter(keys, frame)


# ---- 聚合求值 ---------------------------------------------------------------

def _agg_over_window(vals, agg):
    """对 vals 计算单个聚合值；vals 为空时返回 EMPTY_AGG 规定值。"""
    if not vals:
        return EMPTY_AGG[agg]
    if agg == "count":
        return len(vals)
    if agg == "sum":
        return sum(vals)
    if agg == "avg":
        return sum(vals) / len(vals)
    if agg == "min":
        return min(vals)
    if agg == "max":
        return max(vals)
    if agg == "first_value":
        return vals[0]
    if agg == "last_value":
        return vals[-1]
    raise ValueError("未知聚合函数: %r" % (agg,))


def _eval_sorted(vals, bounds, agg):
    """分区内（已排序）求值。bounds 为逐行 (start, end) 迭代器，边界单调不减。"""
    n = len(vals)
    out = [None] * n
    if agg in ("count", "sum", "avg"):
        # 双指针维护当前帧的元素和（边界单调不减），避免 n+1 长度前缀和数组
        lo = hi = 0
        running = 0
        i = 0
        for s, e in bounds:
            if s > e:
                out[i] = EMPTY_AGG[agg]
            else:
                while hi <= e:
                    running += vals[hi]
                    hi += 1
                while lo < s:
                    running -= vals[lo]
                    lo += 1
                if agg == "count":
                    out[i] = e - s + 1
                elif agg == "sum":
                    out[i] = running
                else:
                    out[i] = running / (e - s + 1)
            i += 1
        return out
    if agg in ("min", "max"):
        dq = deque()          # 单调队列，存下标；窗口右端单调不减
        hi = 0
        better = (lambda a, b: a < b) if agg == "min" else (lambda a, b: a > b)
        i = 0
        for s, e in bounds:
            if s > e:
                out[i] = EMPTY_AGG[agg]
            else:
                while hi <= e:            # 右扩到 e
                    while dq and better(vals[hi], vals[dq[-1]]):
                        dq.pop()
                    dq.append(hi)
                    hi += 1
                while dq and dq[0] < s:   # 弹出窗口左端之外的元素
                    dq.popleft()
                out[i] = vals[dq[0]]
            i += 1
        return out
    if agg == "first_value":
        i = 0
        for s, e in bounds:
            out[i] = EMPTY_AGG[agg] if s > e else vals[s]
            i += 1
        return out
    if agg == "last_value":
        i = 0
        for s, e in bounds:
            out[i] = EMPTY_AGG[agg] if s > e else vals[e]
            i += 1
        return out
    raise ValueError("未知聚合函数: %r" % (agg,))


def evaluate(rows, frame=None, agg="sum",
             partition_key=None, order_key=None, value=None):
    """对一组行计算窗口聚合，结果按**输入顺序**返回。

    参数:
        rows         : 行序列（任意可索引/可调用取值的对象）。
        frame        : Frame；None 表示默认帧 RANGE UNBOUNDED PRECEDING AND CURRENT ROW。
        agg          : 'count'|'sum'|'avg'|'min'|'max'|'first_value'|'last_value'。
        partition_key: 行 -> 分区键；None 表示全表一个分区。
        order_key    : 行 -> 排序键；None 表示按输入顺序（仅 ROWS 模式有意义）。
        value        : 行 -> 聚合值；None 表示行本身就是数值。
    返回:
        与 rows 等长的列表，out[j] 是 rows[j] 的窗口聚合结果。
    可复现性:
        排序键 = (order_key(row), 原始下标)，原始下标作决胜键，Python sort 稳定，
        因此排序键重复（不稳定）时，同一输入多次运行结果完全一致。
    """
    if frame is None:
        frame = DEFAULT_FRAME
    _check_frame(frame)
    if agg not in EMPTY_AGG:
        raise ValueError("未知聚合函数: %r" % (agg,))

    n = len(rows)
    pkf = partition_key if partition_key is not None else (lambda r: None)
    okf = order_key if order_key is not None else (lambda r: 0)
    valf = value if value is not None else (lambda r: r)

    # 稳定排序：分区键 -> 排序键 -> 原始下标（决胜键，保证可复现）。
    # 排序键按需从行取值，不落地 n 长度的全量键/值数组，降低内存峰值；
    # 同键的行由决胜键 j 保证相对顺序固定（Python sort 稳定）。
    order = list(range(n))
    if partition_key is not None or order_key is not None:
        order.sort(key=lambda j: (pkf(rows[j]), okf(rows[j]), j))

    out = [None] * n
    seg_start = 0
    while seg_start < n:
        seg_end = seg_start + 1
        pk = pkf(rows[order[seg_start]])
        while seg_end < n and pkf(rows[order[seg_end]]) == pk:
            seg_end += 1
        idx = order[seg_start:seg_end]
        keys = [okf(rows[j]) for j in idx]
        vals = [valf(rows[j]) for j in idx]
        res = _eval_sorted(vals, _bounds_iter(keys, frame), agg)
        for pos, j in enumerate(idx):
            out[j] = res[pos]
        seg_start = seg_end
    return out


def evaluate_naive(rows, frame=None, agg="sum",
                   partition_key=None, order_key=None, value=None):
    """朴素参照实现：逐行收集帧内元素再聚合。用于对拍，语义与 evaluate 完全一致。"""
    if frame is None:
        frame = DEFAULT_FRAME
    _check_frame(frame)
    n = len(rows)
    pkf = partition_key if partition_key is not None else (lambda r: None)
    okf = order_key if order_key is not None else (lambda r: 0)
    valf = value if value is not None else (lambda r: r)

    order = sorted(range(n), key=lambda j: (pkf(rows[j]), okf(rows[j]), j))
    out = [None] * n
    seg_start = 0
    while seg_start < n:
        seg_end = seg_start + 1
        pk = pkf(rows[order[seg_start]])
        while seg_end < n and pkf(rows[order[seg_end]]) == pk:
            seg_end += 1
        idx = order[seg_start:seg_end]
        keys = [okf(rows[j]) for j in idx]
        vals = [valf(rows[j]) for j in idx]
        starts, ends = frame_bounds(keys, frame)
        for pos, j in enumerate(idx):
            s, e = starts[pos], ends[pos]
            window = [] if s > e else vals[s:e + 1]
            out[j] = _agg_over_window(window, agg)
        seg_start = seg_end
    return out
