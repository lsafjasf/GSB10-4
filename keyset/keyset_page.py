"""keyset_page.py — 键集分页（Keyset / Cursor Pagination）库。

仅依赖 Python 标准库（sqlite3）。

为什么不会越翻越慢
------------------
传统 OFFSET 分页：SELECT ... ORDER BY k LIMIT n OFFSET m
    数据库每次都要从索引头部扫描并丢弃 m 行，单页耗时 O(m)，页号越大越慢。

键集分页：记住上一页最后一行的排序键 (order_col, pk)，下一页直接
    SELECT ... WHERE (order_col, pk) > (?, ?) ORDER BY order_col, pk LIMIT n
    在 (order_col, pk) 索引上做一次 B+ 树定位 O(log N)，再顺序取 n 行。
    单页耗时 O(log N + n)，与页号无关 —— 翻到第 1 页和第 10000 页一样快。

一致性保证（翻页期间数据变化）
------------------------------
游标是"值"而不是"位置"：
  * 在游标之前插入/删除行 -> 不影响后续页，不会重复、不会跳过；
  * 在游标之后插入的行 -> 会在后续页中恰好出现一次；
  * 删除尚未翻到的行 -> 该行不再出现，其余行不受影响。
注意：若 UPDATE 把某行的排序键从游标前改到游标后，该行会再次出现
（这是所有键集分页的固有语义，见 README）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional, Sequence, Tuple

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"非法标识符: {name!r}")
    return name


@dataclass
class Page:
    """一页结果。cursor 为不透明字符串，传给 next_page() 取下一页。"""

    rows: List[Tuple[Any, Any]]          # [(pk, order_col), ...]
    cursor: Optional[str]                # None 表示已是最后一页
    page_no: int

    @property
    def has_next(self) -> bool:
        return self.cursor is not None


def _encode_cursor(key: Any, pk: Any) -> str:
    return json.dumps([key, pk], separators=(",", ":"))


def _decode_cursor(cursor: str) -> Tuple[Any, Any]:
    key, pk = json.loads(cursor)
    return key, pk


class KeysetPaginator:
    """按 (order_col, pk) 升序的键集分页器。

    要求表上存在索引: CREATE INDEX ... ON t(order_col, pk)
    （若 order_col 本身唯一，单列索引亦可，但复合游标仍然安全。）
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        table: str,
        order_col: str,
        pk: str = "id",
        page_size: int = 100,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size 必须 >= 1")
        self.conn = conn
        self.table = _check_ident(table)
        self.order_col = _check_ident(order_col)
        self.pk = _check_ident(pk)
        self.page_size = page_size

        t, c, p = self.table, self.order_col, self.pk
        self._sql_first = (
            f"SELECT {p}, {c} FROM {t} ORDER BY {c}, {p} LIMIT ?"
        )
        # 键集条件： (c, p) > (key, pk)，按复合键做字典序比较。
        # 用行值（row-value）写法而非展开成 OR：OR 会使部分 SQLite 版本放弃
        # 范围 SEARCH、退化为从索引头开始的全 SCAN（实测慢 400 倍）。
        # 行值比较在 (c,p) 复合索引上是一次 B+ 树定位 + 顺序取 n 行：O(log N+n)。
        # 需要 SQLite >= 3.15（2016-10，Python 3.7+ 标配版本均满足）。
        self._sql_next = (
            f"SELECT {p}, {c} FROM {t} "
            f"WHERE ({c}, {p}) > (?, ?) "
            f"ORDER BY {c}, {p} LIMIT ?"
        )

    def _fetch(self, sql: str, params: Sequence[Any], page_no: int) -> Page:
        # 多取 1 行判断是否还有下一页
        rows = self.conn.execute(sql, (*params, self.page_size + 1)).fetchall()
        has_next = len(rows) > self.page_size
        rows = rows[: self.page_size]
        cursor = None
        if has_next and rows:
            last_pk, last_key = rows[-1]
            cursor = _encode_cursor(last_key, last_pk)
        return Page(rows=[(r[0], r[1]) for r in rows], cursor=cursor, page_no=page_no)

    def first_page(self) -> Page:
        """首页。"""
        return self._fetch(self._sql_first, (), 1)

    def next_page(self, cursor: str, page_no: int) -> Page:
        """用上一页的 cursor 取下一页。"""
        key, pk = _decode_cursor(cursor)
        return self._fetch(self._sql_next, (key, pk), page_no)

    def iter_pages(self) -> Iterator[Page]:
        """从首页开始逐页迭代直到末页。"""
        page = self.first_page()
        yield page
        while page.has_next:
            page = self.next_page(page.cursor, page.page_no + 1)
            yield page

    def iter_rows(self) -> Iterator[Tuple[Any, Any]]:
        """从首页开始逐行迭代（内部仍按页取数）。"""
        for page in self.iter_pages():
            yield from page.rows


def offset_page(
    conn: sqlite3.Connection,
    table: str,
    order_col: str,
    pk: str,
    page_size: int,
    page_no: int,
) -> List[Tuple[Any, Any]]:
    """传统 OFFSET 分页（对照组，仅用于测试/基准）。"""
    table, order_col, pk = map(_check_ident, (table, order_col, pk))
    sql = (
        f"SELECT {pk}, {order_col} FROM {table} "
        f"ORDER BY {order_col}, {pk} LIMIT ? OFFSET ?"
    )
    return [
        (r[0], r[1])
        for r in conn.execute(sql, (page_size, (page_no - 1) * page_size))
    ]
