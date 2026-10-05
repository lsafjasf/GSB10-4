"""
engine.py — 关系算子，落实 UNKNOWN / NULL 在各子句中的传递规则。

规则（与 SQL 标准一致，且与 SQLite 行为对拍，见 diff_sqlite.py）：

  WHERE / ON：只有谓词为 TRUE 的行（或行对）保留；FALSE 与 UNKNOWN 都剔除。
              注意：UNKNOWN 在这里"不被选中"，但它依然是 UNKNOWN 而不是 FALSE——
              NOT(UNKNOWN)=UNKNOWN，把它当假会让双重否定等查询出错。
  聚合：
      COUNT(*)          计数输入的全部行，不关心任何列是否为 NULL；
      COUNT(col)        跳过该列为 NULL 的行；
      SUM/AVG/MIN/MAX   跳过 NULL；输入为空或全 NULL 时返回 NULL；
      COUNT 无输入时为 0。
      聚合永远不会"看到" UNKNOWN——UNKNOWN 是谓词的结果，列值只有 NULL/非NULL。
  GROUP BY：所有 NULL 键落入同一个组（NULL 彼此"成组相等"，区别于 NULL=NULL=UNKNOWN）。
  DISTINCT：所有 NULL 视为同一个值，输出里至多一个 NULL（去重相等）。
  ORDER BY：键为 NULL 的行集中放置；位置由 nulls_first 显式决定
            （ASC+NULLS LAST 模拟 PostgreSQL 默认；可切 NULLS FIRST 模拟 SQL Server）。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

from .tvl import (
    And,
    Expr,
    NULL,
    Tri,
    is_null,
    is_true,
    null_norm,
)

Row = dict


# ---------------------------------------------------------------------------
# 过滤 / 连接
# ---------------------------------------------------------------------------

def where(rows: Iterable[Row], pred: Expr, *,
          short_circuit: bool = True, reverse: bool = False) -> list[Row]:
    """WHERE：谓词 = TRUE 才保留。"""
    out = []
    for row in rows:
        if is_true(pred.eval(row, short_circuit=short_circuit, reverse=reverse)
                   if isinstance(pred, And) else pred.eval(row)):
            out.append(row)
    return out


def inner_join(left: Iterable[Row], right: Iterable[Row], on: Expr, *,
               lprefix: str = "", rprefix: str = "") -> list[Row]:
    """INNER JOIN ON：谓词 = TRUE 才配对；FALSE 与 UNKNOWN 都不连接。

    两表同名列加前缀（lprefix/rprefix + 原名）。
    """
    ls = list(left)
    rs = list(right)
    out = []
    for lrow in ls:
        for rrow in rs:
            merged = _merge_rows(lrow, rrow, lprefix, rprefix)
            if is_true(on.eval(merged)):
                out.append(merged)
    return out


def _merge_rows(lrow, rrow, lprefix, rprefix):
    merged = {}
    for k, v in lrow.items():
        merged[(lprefix + k) if lprefix else k] = v
    for k, v in rrow.items():
        merged[(rprefix + k) if rprefix else k] = v
    return merged


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------

def _vals(rows: Iterable[Row], col: str):
    for row in rows:
        v = null_norm(row[col])
        if not is_null(v):
            yield v


def agg_count_star(rows: Iterable[Row]) -> int:
    return sum(1 for _ in rows)


def agg_count(rows: Iterable[Row], col: str) -> int:
    return sum(1 for _ in _vals(rows, col))


def agg_sum(rows: Iterable[Row], col: str):
    s, n = 0, 0
    for v in _vals(rows, col):
        s += v
        n += 1
    return s if n else NULL


def agg_avg(rows: Iterable[Row], col: str):
    s, n = 0, 0
    for v in _vals(rows, col):
        s += v
        n += 1
    return s / n if n else NULL


def agg_min(rows: Iterable[Row], col: str):
    best = NULL
    for v in _vals(rows, col):
        if is_null(best) or v < best:
            best = v
    return best


def agg_max(rows: Iterable[Row], col: str):
    best = NULL
    for v in _vals(rows, col):
        if is_null(best) or v > best:
            best = v
    return best


AGG_FUNCS = {
    "COUNT(*)": agg_count_star,
    "COUNT": agg_count,
    "SUM": agg_sum,
    "AVG": agg_avg,
    "MIN": agg_min,
    "MAX": agg_max,
}


# ---------------------------------------------------------------------------
# 分组 / 去重 / 排序
# ---------------------------------------------------------------------------

def _group_key(row: Row, keys: Sequence[str]):
    # NULL 必须可哈希且彼此相等：用 _group_null 标记替换 None/NULL
    return tuple(_GROUP_NULL if is_null(row[k]) else null_norm(row[k]) for k in keys)


class _GroupNull:
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self):
        return "NULL(group key)"

    def __lt__(self, other):
        # NULL 组在组键展示排序中永远排最前，仅用于结果稳定展示
        return self is not other


_GROUP_NULL = _GroupNull()


def group_by(rows: Iterable[Row], keys: Sequence[str],
             aggs: dict[str, Callable[[list[Row]], Any]]) -> list[Row]:
    """按 keys 分组；NULL 键全部进入同一组。aggs: 输出列 -> 聚合函数。"""
    groups: dict = {}
    order = []
    for row in rows:
        gk = _group_key(row, keys)
        if gk not in groups:
            groups[gk] = []
            order.append(gk)
        groups[gk].append(row)

    out = []
    for gk in order:
        members = groups[gk]
        result = {}
        for k, raw in zip(keys, gk):
            result[k] = NULL if raw is _GROUP_NULL else raw
        for out_col, fn in aggs.items():
            result[out_col] = fn(members)
        out.append(result)
    return out


def distinct(rows: Iterable[Row], keys: Sequence[str] | None = None) -> list[Row]:
    """按整行（默认）或 keys 去重；NULL 按相等处理，只保留一个。"""
    seen = set()
    out = []
    for row in rows:
        if keys is None:
            sig = tuple(sorted((k, _DIST_NULL if is_null(v) else v)
                               for k, v in row.items()))
        else:
            sig = tuple(_DIST_NULL if is_null(row[k]) else null_norm(row[k])
                        for k in keys)
        if sig not in seen:
            seen.add(sig)
            out.append(dict(row))
    return out


class _DistNull(_GroupNull):
    def __repr__(self):
        return "NULL"


_DIST_NULL = _DistNull()


def order_by(rows: Iterable[Row], key: str, *,
             ascending: bool = True, nulls_first: bool = False) -> list[Row]:
    """单列排序。NULL 集中放置：nulls_first=False -> 末尾，True -> 开头。

    非空值之间按常规比较；NULL 组内部与非空组内部各自保持稳定（stable sort）。
    """
    nonnull = [r for r in rows if not is_null(r[key])]
    nulls = [r for r in rows if is_null(r[key])]
    nonnull.sort(key=lambda r: null_norm(r[key]), reverse=not ascending)
    return (nulls + nonnull) if nulls_first else (nonnull + nulls)
