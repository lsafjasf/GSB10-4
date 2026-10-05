"""nulljoin: 多表关联中的空外键处理库（仅标准库）。

核心规则（SQL 语义）：
    空外键（None）不得与任何值匹配成功，包括与另一个空值比较。
    即连接谓词 `a = b` 在任一侧为 NULL 时结果不是 TRUE，连接不成立。

两类实现：
    - `join`            生产实现：哈希连接，O(n + m)。
    - `reference_join`  参考实现：朴素嵌套循环，O(n * m)，逻辑直白，
                        用于与生产实现逐行对拍。

两类实现的输出都遵循同一套稳定顺序（见 `join` 的 docstring），
因此可以逐字节比较。
"""

from enum import Enum

__all__ = ["JoinType", "join", "reference_join", "row_width"]


class JoinType(Enum):
    INNER = "inner"
    LEFT = "left"
    RIGHT = "right"
    FULL = "full"
    LEFT_SEMI = "left_semi"
    LEFT_ANTI = "left_anti"


def row_width(row, width):
    """把行规整为定长元组，短了补 None，便于输出宽度稳定。"""
    if len(row) == width:
        return tuple(row)
    if len(row) < width:
        return tuple(row) + (None,) * (width - len(row))
    return tuple(row[:width])


def _null_padding(width):
    return (None,) * width


def join(left_rows, right_rows, left_on, right_on, how=JoinType.INNER):
    """生产实现：哈希连接。

    参数：
        left_rows / right_rows: 可迭代的序列行（tuple/list）。
        left_on / right_on:     连接键在各自行中的列下标。
        how:                    JoinType。

    空外键规则：
        键为 None 的行永不进入匹配集；None == None 也不成立。

    稳定输出顺序（消除顺序不确定性）：
        INNER     按左表行序；同一左行命中的右行按右表行序。
        LEFT      左表行序；未命中的左行在右表列位置补 None。
        RIGHT     右表行序；未命中的右行在左表列位置补 None。
        FULL      先按左表行序输出（命中或左补空），
                  再按右表行序追加未命中的右行（左表列补 None）。
        LEFT_SEMI 左表行序，只保留至少命中一次的左行（每行一次）。
        LEFT_ANTI 左表行序，只保留从未命中的左行（含键为 None 的左行）。
    """
    how = JoinType(how)
    left_rows = [tuple(r) for r in left_rows]
    right_rows = [tuple(r) for r in right_rows]
    lw = max((len(r) for r in left_rows), default=0)
    rw = max((len(r) for r in right_rows), default=0)

    # 1) 右表建哈希桶：键 -> 右行下标列表。None 键直接丢弃，永不匹配。
    buckets = {}
    for j, rrow in enumerate(right_rows):
        key = rrow[right_on]
        if key is None:
            continue
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = [j]
        else:
            bucket.append(j)

    # RIGHT：对左表建桶，按右表行序输出（命中行 + 未命中补空行）。
    if how is JoinType.RIGHT:
        lbuckets = {}
        for i, lrow in enumerate(left_rows):
            key = lrow[left_on]
            if key is None:
                continue
            bucket = lbuckets.get(key)
            if bucket is None:
                lbuckets[key] = [i]
            else:
                bucket.append(i)
        out = []
        pad = _null_padding(lw)
        for rrow in right_rows:
            key = rrow[right_on]
            hits = lbuckets.get(key) if key is not None else None
            if hits:
                for i in hits:
                    out.append(left_rows[i] + rrow)
            else:
                out.append(pad + rrow)
        return out

    # 2) 按左表行序探测（INNER / LEFT / FULL）。
    matched_right = bytearray(len(right_rows)) if how is JoinType.FULL else None

    if how in (JoinType.LEFT_SEMI, JoinType.LEFT_ANTI):
        # 半连接 / 反连接：只输出左行，宽度为左表宽度。
        out = []
        want_hit = how is JoinType.LEFT_SEMI
        for lrow in left_rows:
            key = lrow[left_on]
            hit = key is not None and key in buckets
            if hit == want_hit:
                out.append(lrow)
        return out

    out = []
    for lrow in left_rows:
        key = lrow[left_on]
        hits = buckets.get(key) if key is not None else None
        if hits:
            for j in hits:
                out.append(lrow + right_rows[j])
                if matched_right is not None:
                    matched_right[j] = 1
        elif how in (JoinType.LEFT, JoinType.FULL):
            out.append(lrow + _null_padding(rw))
        # INNER：未命中即丢弃。

    if matched_right is not None:
        # FULL：按右表行序追加未命中的右行，左表列补 None。
        pad = _null_padding(lw)
        for j, rrow in enumerate(right_rows):
            if not matched_right[j]:
                out.append(pad + rrow)
    return out


def reference_join(left_rows, right_rows, left_on, right_on, how=JoinType.INNER):
    """参考实现：朴素嵌套循环，逻辑直白、与生产实现独立编码。

    空外键规则：`_eq` 是唯一判等入口，任一侧为 None 即不成立。
    输出顺序与 `join` 完全一致（同一套稳定顺序规范）。
    """
    how = JoinType(how)
    left_rows = [tuple(r) for r in left_rows]
    right_rows = [tuple(r) for r in right_rows]
    lw = max((len(r) for r in left_rows), default=0)
    rw = max((len(r) for r in right_rows), default=0)

    def _eq(a, b):
        # 空外键不得与任何值匹配成功，包括与另一个空值比较。
        if a is None or b is None:
            return False
        return a == b

    matched_right = bytearray(len(right_rows))
    out = []

    if how is JoinType.RIGHT:
        pad = _null_padding(lw)
        for rrow in right_rows:
            hit = False
            for lrow in left_rows:
                if _eq(lrow[left_on], rrow[right_on]):
                    hit = True
                    out.append(lrow + rrow)
            if not hit:
                out.append(pad + rrow)
        return out

    if how in (JoinType.LEFT_SEMI, JoinType.LEFT_ANTI):
        for lrow in left_rows:
            hit = any(_eq(lrow[left_on], rrow[right_on]) for rrow in right_rows)
            if how is JoinType.LEFT_SEMI and hit:
                out.append(lrow)
            elif how is JoinType.LEFT_ANTI and not hit:
                out.append(lrow)
        return out

    for lrow in left_rows:
        hit = False
        for j, rrow in enumerate(right_rows):
            if _eq(lrow[left_on], rrow[right_on]):
                hit = True
                matched_right[j] = 1
                if how in (JoinType.INNER, JoinType.LEFT, JoinType.FULL):
                    out.append(lrow + rrow)
        if not hit and how in (JoinType.LEFT, JoinType.FULL):
            out.append(lrow + _null_padding(rw))

    if how is JoinType.FULL:
        pad = _null_padding(lw)
        for j, rrow in enumerate(right_rows):
            if not matched_right[j]:
                out.append(pad + rrow)
    return out
