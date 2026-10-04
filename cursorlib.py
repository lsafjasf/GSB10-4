"""Stable keyset (cursor) pagination over sqlite3. Stdlib only.

Guarantee under concurrent insert/delete:
  * NO DUPLICATES: any row is returned at most once per scan, because the
    cursor position (sort_key, row_id) advances strictly monotonically.
  * NO MISSES for rows that exist (with an unchanged sort key) for the whole
    duration of the scan: when the cursor reaches their key range they are
    still there, so they are returned exactly once.
  * Rows inserted during the scan appear iff their sort position is after the
    cursor at insert time; rows deleted before the cursor reaches them do not
    appear. Both cases are explainable point differences (see demo_diff.py).

Cursor token format:  c1.<b64url(payload)>.<b64url(sig)>
  payload = compact JSON {"v":1,"k":<sort_key>,"i":<row_id>}
  sig     = HMAC-SHA256(secret, payload)[:16]
Any tampering or truncation fails signature / schema validation and is
rejected with CursorError -- never resolved to a "close enough" position.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional, Sequence, Tuple

_CURSOR_PREFIX = "c1"
_SIG_BYTES = 16  # truncated HMAC-SHA256 (128-bit)
_B64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CursorError(ValueError):
    """Cursor token is malformed, truncated, forged, or schema-invalid."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    if not text or any(ch not in _B64URL_ALPHABET for ch in text):
        raise CursorError("cursor segment is not valid unpadded base64url")
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:  # binascii.Error, ValueError
        raise CursorError("cursor segment failed base64url decode") from exc


def _check_scalar(value: Any, name: str) -> None:
    if isinstance(value, bool):
        raise CursorError(f"cursor field {name!r} must not be bool")
    if not isinstance(value, (int, float, str)):
        raise CursorError(f"cursor field {name!r} must be int/float/str")
    if isinstance(value, float) and not math.isfinite(value):
        raise CursorError(f"cursor field {name!r} must be finite")


def encode_cursor(key: Any, row_id: Any, secret: bytes) -> str:
    """Encode a keyset position (sort key + unique tie-breaker id)."""
    _check_scalar(key, "k")
    _check_scalar(row_id, "i")
    payload = json.dumps(
        {"v": 1, "k": key, "i": row_id}, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    sig = hmac.new(secret, payload, hashlib.sha256).digest()[:_SIG_BYTES]
    return f"{_CURSOR_PREFIX}.{_b64url_encode(payload)}.{_b64url_encode(sig)}"


def decode_cursor(token: str, secret: bytes) -> Tuple[Any, Any]:
    """Decode and verify a cursor token. Raises CursorError on any problem."""
    if not isinstance(token, str):
        raise CursorError("cursor must be a string")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _CURSOR_PREFIX:
        raise CursorError("cursor must have form c1.<payload>.<sig>")
    payload = _b64url_decode(parts[1])
    sig = _b64url_decode(parts[2])
    if len(sig) != _SIG_BYTES:
        raise CursorError("cursor signature has wrong length")
    expected = hmac.new(secret, payload, hashlib.sha256).digest()[:_SIG_BYTES]
    if not hmac.compare_digest(sig, expected):
        raise CursorError("cursor signature mismatch (tampered or wrong secret)")
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError("cursor payload is not valid JSON") from exc
    if not isinstance(obj, dict) or set(obj) != {"v", "k", "i"} or obj["v"] != 1:
        raise CursorError("cursor payload schema mismatch")
    _check_scalar(obj["k"], "k")
    _check_scalar(obj["i"], "i")
    return obj["k"], obj["i"]


def _ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


@dataclass
class Page:
    items: List[Tuple[Any, Any]]  # [(row_id, sort_key), ...]
    next_cursor: Optional[str]    # None => this was the last page


def fetch_page(
    conn: sqlite3.Connection,
    *,
    table: str,
    key_col: str,
    id_col: str,
    secret: bytes,
    cursor: Optional[str] = None,
    limit: int = 100,
    extra_where: str = "",
    params: Sequence[Any] = (),
) -> Page:
    """Fetch one forward page. cursor=None means the first page.

    Rows are ordered by (key_col, id_col); id_col must be unique and
    non-NULL so ties on key_col resolve to a stable total order.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    table, key_col, id_col = _ident(table), _ident(key_col), _ident(id_col)
    clauses: List[str] = []
    args: List[Any] = list(params)
    if extra_where:
        clauses.append(f"({extra_where})")
    if cursor is not None:
        key, row_id = decode_cursor(cursor, secret)
        clauses.append(f"({key_col}, {id_col}) > (?, ?)")
        args.extend([key, row_id])
    sql = f"SELECT {id_col}, {key_col} FROM {table}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" ORDER BY {key_col}, {id_col} LIMIT ?"
    args.append(limit + 1)  # one extra row to detect "has next page"
    rows = conn.execute(sql, args).fetchall()
    has_next = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_cursor(rows[-1][1], rows[-1][0], secret) if rows and has_next else None
    )
    return Page(items=[(r[0], r[1]) for r in rows], next_cursor=next_cursor)


def iter_pages(conn: sqlite3.Connection, **kw: Any) -> Iterator[Page]:
    """Yield pages from the first to the last (inclusive).

    Accepts the same keyword arguments as fetch_page (including limit).
    """
    cursor: Optional[str] = kw.pop("cursor", None)
    while True:
        page = fetch_page(conn, cursor=cursor, **kw)
        yield page
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def iter_all_ids(conn: sqlite3.Connection, **kw: Any) -> List[Any]:
    """Convenience: collect all row ids across the whole scan."""
    return [row_id for page in iter_pages(conn, **kw) for row_id, _ in page.items]
