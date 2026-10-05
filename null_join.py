"""null_join: 多表关联中的空外键(NULL FK)处理库。

仅使用 Python 标准库。行用 tuple 表示，NULL 用 None 表示。

核心语义（与 SQL 一致）：
    NULL 不等于任何值，包括 NULL 本身。因此空外键在任何等值关联中
    都不会匹配成功。但不同关联类型对"未匹配行"的处理各不相同：

    - inner_join : 只保留匹配成功的行对；空外键行被丢弃。
    - left_join  : 左表全部保留；空外键左行右側补 NULL。
    - right_join : 右表全部保留；右表 NULL 键行左侧补 NULL。
    - full_join  : 两表都全部保留；空外键行另一侧补 NULL。
    - semi_join  : 只保留"存在匹配"的左行；空外键左行被丢弃。
    - anti_join  : 只保留"不存在匹配"的左行；空外键左行【保留】
                   （NOT EXISTS 语义，区别于 SQL NOT IN 的陷阱）。

顺序确定性：
    哈希关联的输出顺序依赖实现细节。sort_rows() 提供与值无关的
    规范排序（NULL 固定排在非 NULL 之后），用于消除顺序不确定性，
    使结果可逐行对拍。
"""

__all__ = [
    "inner_join", "left_join", "right_join", "full_join",
    "semi_join", "anti_join", "sort_rows",
]

_MISSING = object()


def _keyfunc(key):
    """key 可以是列下标或 callable，统一成 callable。"""
    if callable(key):
        return key
    idx = key
    return lambda row: row[idx]


def _width(rows, explicit):
    if explicit is not None:
        return explicit
    for row in rows:
        return len(row)
    return 0


def _build_index(right, rkey):
    """右表非 NULL 键的哈希索引；NULL 键行不进索引（永不匹配）。"""
    index = {}
    for r in right:
        k = rkey(r)
        if k is None:
            continue
        bucket = index.get(k)
        if bucket is None:
            index[k] = [r]
        else:
            bucket.append(r)
    return index


def inner_join(left, right, lkey, rkey):
    """内连接：仅输出键相等且两侧键均非 NULL 的行对。"""
    lk, rk = _keyfunc(lkey), _keyfunc(rkey)
    index = _build_index(right, rk)
    out = []
    for l in left:
        k = lk(l)
        if k is None:  # 空外键永不匹配，包括 NULL = NULL
            continue
        for r in index.get(k, ()):
            out.append(l + r)
    return out


def left_join(left, right, lkey, rkey, rwidth=None):
    """左外连接：左表全保留；未匹配（含空外键）左行右侧补 NULL。"""
    lk, rk = _keyfunc(lkey), _keyfunc(rkey)
    rw = _width(right, rwidth)
    null_right = (None,) * rw
    index = _build_index(right, rk)
    out = []
    for l in left:
        k = lk(l)
        matched = index.get(k) if k is not None else None
        if matched:
            for r in matched:
                out.append(l + r)
        else:
            out.append(l + null_right)
    return out


def right_join(left, right, lkey, rkey, lwidth=None):
    """右外连接：右表全保留；未匹配（含 NULL 键）右行左侧补 NULL。"""
    lk, rk = _keyfunc(lkey), _keyfunc(rkey)
    lw = _width(left, lwidth)
    null_left = (None,) * lw
    index = _build_index(left, lk)  # 右外连接：对左表非 NULL 键建索引
    out = []
    for r in right:
        k = rk(r)
        matched = index.get(k) if k is not None else None
        if matched:
            for l in matched:
                out.append(l + r)
        else:
            out.append(null_left + r)
    return out


def full_join(left, right, lkey, rkey, lwidth=None, rwidth=None):
    """全外连接：两表全保留；未匹配侧补 NULL；空外键永不匹配。"""
    lk, rk = _keyfunc(lkey), _keyfunc(rkey)
    lw = _width(left, lwidth)
    rw = _width(right, rwidth)
    null_left = (None,) * lw
    null_right = (None,) * rw
    index = _build_index(right, rk)
    out = []
    matched_right_ids = set()
    for l in left:
        k = lk(l)
        matched = index.get(k) if k is not None else None
        if matched:
            for r in matched:
                out.append(l + r)
                matched_right_ids.add(id(r))
        else:
            out.append(l + null_right)
    for r in right:
        if id(r) not in matched_right_ids:
            out.append(null_left + r)
    return out


def semi_join(left, right, lkey, rkey):
    """半连接（EXISTS / IN 语义）：保留存在匹配的左行，不展开右表。

    空外键左行不存在匹配（NULL 不等于任何值），因此被丢弃。
    """
    lk, rk = _keyfunc(lkey), _keyfunc(rkey)
    keys = set()
    for r in right:
        k = rk(r)
        if k is not None:
            keys.add(k)
    return [l for l in left if lk(l) is not None and lk(l) in keys]


def anti_join(left, right, lkey, rkey):
    """反半连接（NOT EXISTS 语义）：保留不存在匹配的左行。

    空外键左行不可能存在匹配，因此【保留】。
    注意：这不同于 SQL 的 NOT IN —— 右表键列含 NULL 时 NOT IN 会
    因三值逻辑返回空集，而 NOT EXISTS / 本实现不会。
    """
    lk, rk = _keyfunc(lkey), _keyfunc(rkey)
    keys = set()
    for r in right:
        k = rk(r)
        if k is not None:
            keys.add(k)
    return [l for l in left if lk(l) is None or lk(l) not in keys]


def _cell_key(v):
    # None 固定排在非 None 之后；非 None 按 (类型名, 值) 排序，
    # 避免混合类型（如 int 与 str）比较抛 TypeError。
    if v is None:
        return (1, "", "")
    return (0, type(v).__name__, v if isinstance(v, (int, float, str)) else repr(v))


def sort_rows(rows):
    """规范排序：消除关联结果的顺序不确定性，输出稳定可逐行对拍。"""
    return sorted(rows, key=lambda row: tuple(_cell_key(c) for c in row))
