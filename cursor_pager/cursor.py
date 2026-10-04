"""Tamper-evident cursor encoding.

Cursor format (ASCII)::

    v1.<payload_b64url>.<tag_b64url>

* ``v1``             -- version tag, must match exactly.
* ``payload_b64url`` -- base64url(no padding) of canonical JSON::

      {"k": <sort key>, "i": <tiebreaker>, "q": "<16-hex query fingerprint>"}

* ``tag_b64url``     -- base64url of the full 32-byte HMAC-SHA256 tag
  computed as ``HMAC-SHA256(secret, "v1." + payload_b64url)``.

Every token is validated structurally and cryptographically before any value
is returned to the caller: a flipped bit, a truncated tail, a wrong version,
malformed base64, non-JSON/wrong-shape payloads, a foreign query fingerprint
or a wrong secret all raise ``CursorError`` -- there is no code path where a
garbled token decodes into a plausible position.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any, Mapping

VERSION = "v1"
_MAX_PAYLOAD_BYTES = 4096
_B64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_HEX_ALPHABET = frozenset("0123456789abcdef")


class CursorError(ValueError):
    """Raised for any malformed, truncated, tampered or foreign cursor."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    if not text or not text.isascii():
        raise CursorError("cursor segment is not ascii/base64url")
    if any(ch not in _B64URL_ALPHABET for ch in text):
        raise CursorError("cursor segment is not ascii/base64url")
    padded = text + ("=" * (-len(text) % 4))
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise CursorError("cursor segment is not ascii/base64url") from exc


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def query_fingerprint(
    table: str,
    sort_col: str,
    id_col: str,
    descending: bool = False,
    where_sql: str | None = None,
    params: tuple[Any, ...] = (),
) -> str:
    """Stable 16-hex fingerprint binding a cursor to one exact query shape.

    A cursor minted for one table/sort/filter combination is rejected when
    replayed against another.  Params participate in the fingerprint, so a
    cursor for ``status='open'`` cannot be replayed against ``status='closed'``.
    """
    shape = {
        "table": table,
        "sort_col": sort_col,
        "id_col": id_col,
        "descending": bool(descending),
        "where_sql": where_sql or "",
        "params": [_jsonable(p) for p in params],
    }
    digest = hashlib.sha256(_canonical_json(shape)).hexdigest()
    return digest[:16]


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise CursorError("non-finite cursor parameter")
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": _b64url_encode(bytes(value))}
    raise CursorError(f"unsupported parameter type: {type(value).__name__}")


def encode_cursor(
    sort_value: Any,
    tiebreaker: Any,
    query_fp: str,
    secret: bytes | str,
) -> str:
    """Build a signed cursor for the row a page should continue after."""
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, (bytes, bytearray)) or not secret:
        raise ValueError("secret must be a non-empty bytes/str value")
    payload = {
        "k": _jsonable(sort_value),
        "i": _jsonable(tiebreaker),
        "q": _validate_fp(query_fp),
    }
    body = _canonical_json(payload)
    if len(body) > _MAX_PAYLOAD_BYTES:
        raise CursorError("cursor payload too large")
    body_seg = _b64url_encode(body)
    tag = hmac.new(
        secret, f"{VERSION}.{body_seg}".encode("ascii"), hashlib.sha256
    ).digest()
    return f"{VERSION}.{body_seg}.{_b64url_encode(tag)}"


def _validate_fp(fp: Any) -> str:
    if not isinstance(fp, str) or len(fp) != 16:
        raise CursorError("bad query fingerprint")
    if any(ch not in _HEX_ALPHABET for ch in fp):
        raise CursorError("bad query fingerprint")
    return fp


def decode_cursor(
    token: Any,
    query_fp: str,
    secret: bytes | str,
) -> tuple[Any, Any]:
    """Validate a cursor and return ``(sort_value, tiebreaker)``.

    Raises ``CursorError`` for *every* deviation from the expected shape,
    including tokens that structurally parse but fail the HMAC check.
    """
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, (bytes, bytearray)) or not secret:
        raise ValueError("secret must be a non-empty bytes/str value")

    if not isinstance(token, str) or not token:
        raise CursorError("cursor must be a non-empty string")
    parts = token.split(".")
    if len(parts) != 3:
        raise CursorError("cursor must have exactly 3 segments")
    version, body_seg, tag_seg = parts
    if version != VERSION:
        raise CursorError(f"unsupported cursor version: {version!r}")

    body = _b64url_decode(body_seg)
    if not body or len(body) > _MAX_PAYLOAD_BYTES:
        raise CursorError("cursor payload out of size bounds")
    supplied_tag = _b64url_decode(tag_seg)
    if len(supplied_tag) != hashlib.sha256().digest_size:
        raise CursorError("cursor tag has wrong length")

    expected_tag = hmac.new(
        secret, f"{VERSION}.{body_seg}".encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected_tag, supplied_tag):
        raise CursorError("cursor signature mismatch")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError("cursor payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CursorError("cursor payload must be a JSON object")
    if set(payload.keys()) != {"k", "i", "q"}:
        raise CursorError("cursor payload has unexpected fields")

    sort_value = payload["k"]
    tiebreaker = payload["i"]
    if not _is_scalar(sort_value) or not _is_scalar(tiebreaker):
        raise CursorError("cursor position values must be JSON scalars")
    if payload["q"] != _validate_fp(query_fp):
        raise CursorError("cursor does not belong to this query")
    return sort_value, tiebreaker


def _is_scalar(value: Any) -> bool:
    # NOTE: None is deliberately rejected -- keyset comparison needs NOT NULL
    # sort/tiebreaker columns; a null boundary could silently skip rows.
    return isinstance(value, (str, int, float, bool)) and not (
        isinstance(value, float) and value != value
    )
