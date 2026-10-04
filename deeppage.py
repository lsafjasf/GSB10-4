"""深度翻页库：基于 (k, id) 复合索引的键集分页（keyset pagination）。

只使用 Python 标准库（sqlite3）。

为什么不会越翻越慢：
  OFFSET n 分页每翻一页都要从索引头部重新数过 n 行，第 p 页代价是 O(p * page_size)，
  所以页号越大越慢。键集分页把"上一页最后一行的排序键"作为游标，
  下一页直接以 (k, id) > (?, ?) 为条件在 (k, id) 复合索引上做一次范围扫描，
  每页只读取 page_size 行，代价 O(page_size)，与页号无关。
"""

from __future__ import annotations

import random
import sqlite3
import time
from typing import List, Optional, Tuple

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS items (
    id      INTEGER PRIMARY KEY,
    k       INTEGER NOT NULL,
    payload TEXT NOT NULL
);
"""

# 排序键 (k, id) 的复合索引：k 允许重复，用主键 id 做最终决胜，保证全序
INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_items_k_id ON items (k, id);"

# 键集条件：行值比较 (k, id) > (?, ?)。
# SQLite 会把它编译成对 (k, id) 复合索引的单次范围扫描（SEARCH ... USING INDEX），
# 每页只读 page_size 个索引项；不要用 OR 展开形式，优化器会退化成有序扫描+过滤。
KEYSET_WHERE = "(k, id) > (?, ?)"

SELECT_COLS = "id, k, payload"

Row = Tuple[int, int, str]          # (id, k, payload)
Cursor = Tuple[int, int]            # (k, id)


def connect(path: str) -> sqlite3.Connection:
    """autocommit 模式，便于显式控制快照事务；WAL 允许读写并发。"""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA_SQL)


def create_index(conn: sqlite3.Connection) -> None:
    conn.execute(INDEX_SQL)


def seed(conn: sqlite3.Connection, n: int, k_mod: int = 1_000_003,
         batch: int = 100_000, seed_value: int = 42) -> None:
    """确定性造数：k 在 [0, k_mod) 内随机（含大量重复，考验决胜键）。"""
    rng = random.Random(seed_value)
    conn.execute("PRAGMA synchronous=OFF;")
    payloads = [f"payload-{i}" for i in range(1024)]
    inserted = 0
    to_insert = n
    while to_insert > 0:
        m = min(batch, to_insert)
        rows = [(rng.randrange(k_mod), payloads[rng.randrange(1024)])
                for _ in range(m)]
        conn.execute("BEGIN;")
        conn.executemany("INSERT INTO items (k, payload) VALUES (?, ?);", rows)
        conn.execute("COMMIT;")
        to_insert -= m
        inserted += m
        print(f"\r  seeding {inserted}/{n}", end="", flush=True)
    print()


class KeysetPager:
    """键集分页器。每页 O(page_size)，耗时与页号无关。

    snapshot=True 时在构造处开启只读快照事务：整个翻页过程看到同一数据快照，
    期间其他连接的增删改不会造成边界重复或跳过。
    snapshot=False（实时模式）时保证不重复、键严格递增；
    翻页期间插入到游标之后的新行会被读到，插入到游标之前的不会重复出现。
    """

    def __init__(self, conn: sqlite3.Connection, page_size: int = 1000,
                 snapshot: bool = False):
        self.conn = conn
        self.page_size = page_size
        self.snapshot = snapshot
        self.cursor: Optional[Cursor] = None
        self.page_no = 0
        self._in_txn = False
        if snapshot:
            self.conn.execute("BEGIN;")
            self.conn.execute("SELECT id FROM items LIMIT 1;").fetchall()
            self._in_txn = True

    def next_page(self) -> Tuple[List[Row], float]:
        """返回 (rows, 本页耗时秒)。rows 为空表示已到末页之后。"""
        if self.cursor is None:
            sql = (f"SELECT {SELECT_COLS} FROM items "
                   f"ORDER BY k, id LIMIT ?;")
            args: tuple = (self.page_size,)
        else:
            sql = (f"SELECT {SELECT_COLS} FROM items WHERE {KEYSET_WHERE} "
                   f"ORDER BY k, id LIMIT ?;")
            k, i = self.cursor
            args = (k, i, self.page_size)
        t0 = time.perf_counter()
        rows = self.conn.execute(sql, args).fetchall()
        elapsed = time.perf_counter() - t0
        if rows:
            last = rows[-1]
            self.cursor = (last[1], last[0])
        self.page_no += 1
        return rows, elapsed

    def close(self) -> None:
        if self._in_txn:
            self.conn.execute("COMMIT;")
            self._in_txn = False

    def __enter__(self) -> "KeysetPager":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class OffsetPager:
    """传统 OFFSET 分页器（对照组）。第 p 页代价 O(p * page_size)，越翻越慢。"""

    def __init__(self, conn: sqlite3.Connection, page_size: int = 1000):
        self.conn = conn
        self.page_size = page_size
        self.page_no = 0

    def next_page(self) -> Tuple[List[Row], float]:
        sql = (f"SELECT {SELECT_COLS} FROM items "
               f"ORDER BY k, id LIMIT ? OFFSET ?;")
        args = (self.page_size, self.page_no * self.page_size)
        t0 = time.perf_counter()
        rows = self.conn.execute(sql, args).fetchall()
        elapsed = time.perf_counter() - t0
        self.page_no += 1
        return rows, elapsed


def explain_keyset_plan(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        f"EXPLAIN QUERY PLAN SELECT id FROM items WHERE {KEYSET_WHERE} "
        f"ORDER BY k, id LIMIT ?;", (0, 0, 1)).fetchall()
    return "\n".join(r[-1] for r in rows)
