"""Keyset (seek) pagination over a stdlib ``sqlite3`` connection.

Why keyset and not OFFSET/LIMIT
--------------------------------
An OFFSET cursor only stores an integer row *number*. Under concurrent
inserts/deletes that number shifts between requests, so rows are skipped or
re-read.  A keyset cursor stores the *value* of the last delivered row, using
the composite order ``(sort_col, id_col)`` where ``id_col`` is unique; moving
forward is a strict ``>`` comparison on that tuple.  As a result:

* **no duplicates** -- the boundary is strict and the composite key is unique;
* **no misses for stable rows** -- every row that exists at scan start, is not
  deleted during the scan, and matches the query is passed exactly once;
* rows inserted/deleted mid-scan are explained away deterministically
  (see ``diff_experiment.py``) instead of corrupting the window.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from .cursor import CursorError, decode_cursor, encode_cursor, query_fingerprint

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class QuerySpec:
    """Exact shape of one paginated query.

    ``id_col`` must be unique and NOT NULL; both key columns must be NOT NULL.
    """

    table: str
    sort_col: str
    id_col: str
    descending: bool = False
    where_sql: str | None = None
    params: tuple[Any, ...] = ()

    def fingerprint(self) -> str:
        return query_fingerprint(
            self.table,
            self.sort_col,
            self.id_col,
            self.descending,
            self.where_sql,
            tuple(self.params),
        )


@dataclass
class Page:
    rows: list[dict[str, Any]]
    next_cursor: str | None
    has_more: bool


def _ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f'"{name}"'


def fetch_page(
    conn: sqlite3.Connection,
    spec: QuerySpec,
    page_size: int,
    secret: bytes | str,
    cursor_token: str | None = None,
) -> Page:
    """Fetch one page; ``cursor_token=None`` means the first page.

    Internally fetches ``page_size + 1`` rows so ``has_more`` /
    ``next_cursor`` are exact: the final full page returns ``next_cursor=None``
    rather than discovering emptiness on the next request.
    """
    if not isinstance(page_size, int) or page_size <= 0:
        raise ValueError("page_size must be a positive integer")

    table = _ident(spec.table)
    sort_col = _ident(spec.sort_col)
    id_col = _ident(spec.id_col)
    fp = spec.fingerprint()

    clauses: list[str] = []
    bind: list[Any] = list(spec.params)
    if spec.where_sql:
        clauses.append(f"({spec.where_sql})")

    if cursor_token is not None:
        sort_value, tiebreaker = decode_cursor(cursor_token, fp, secret)
        cmp = "<" if spec.descending else ">"
        # Row-value comparison: semantically identical to
        #   (k > ?) OR (k = ? AND id > ?)
        # but SQLite turns it into an index range seek (SEARCH) instead of
        # a full index scan -- this is what keeps deep pages O(log n).
        clauses.append(f"({sort_col}, {id_col}) {cmp} (?, ?)")
        bind.extend([sort_value, tiebreaker])

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    direction = "DESC" if spec.descending else "ASC"
    sql = (
        f"SELECT * FROM {table}{where} "
        f"ORDER BY {sort_col} {direction}, {id_col} {direction} "
        f"LIMIT ?"
    )
    bind.append(page_size + 1)

    cur = conn.execute(sql, bind)
    columns = [d[0] for d in cur.description]
    fetched = cur.fetchall()
    has_more = len(fetched) > page_size
    page_rows = fetched[:page_size]
    rows = [dict(zip(columns, row)) for row in page_rows]

    next_token: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_token = encode_cursor(
            last[spec.sort_col], last[spec.id_col], fp, secret
        )
    return Page(rows=rows, next_cursor=next_token, has_more=has_more)


def drain(
    conn: sqlite3.Connection,
    spec: QuerySpec,
    page_size: int,
    secret: bytes | str,
) -> list[dict[str, Any]]:
    """Convenience helper: walk every page and return all rows."""
    out: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        page = fetch_page(conn, spec, page_size, secret, token)
        out.extend(page.rows)
        if page.next_cursor is None:
            assert not page.has_more, "has_more without a cursor is impossible"
            break
        token = page.next_cursor
    return out
