"""Collision-resistant cache key construction from HTTP request headers.

Design goals (see docs/NORMALIZATION.md for the full rationale):

1. Only an explicit allow-list of header names participates in the key.
2. Header *names* are case-insensitive; header *values* are never case-folded
   unless a per-header policy explicitly declares it (only on RFC-token
   syntax, never inside quoted-strings).
3. Headers are merged in arrival order, then emitted in a fixed (sorted) order,
   so request header reordering never changes the key.
4. The canonical form is a self-delimiting netstring-style encoding, which is
   injective: distinct normalized tuples always serialize to distinct bytes.
5. There is an explicit upper bound. Over-long keys are either content-hashed
   (with a distinct prefix) or rejected; they are never truncated.

Only the Python standard library is used.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple, Union

VERSION = b"HK1"
"""Version tag; bump when the canonicalization rules change incompatibly."""

MAX_CANONICAL_BYTES = 4096
"""Default upper bound on the raw canonical payload (bytes), before encoding."""

#: Headers that participate by default.  "Vary"-driven callers should pass
#: their own explicit list (the server's own Vary header names).
DEFAULT_PARTICIPATING: Tuple[str, ...] = (
    "accept",
    "accept-encoding",
    "accept-language",
    "authorization",
    "content-encoding",
    "content-language",
    "content-type",
    "cookie",
    "x-api-key",
)

#: Normalization policy per header name.
#:
#:   "raw"          : trim leading/trailing OWS only; preserve inner content
#:   "token_list"   : comma-list; trim each element; case-fold bare RFC tokens,
#:                    preserve quoted-strings verbatim; keep order + duplicates
#:   "token_set"    : like token_list, but elements are sorted and deduplicated
#:                    (only for headers where element order/duplicates are
#:                    semantically irrelevant, e.g. Accept-Encoding codings)
#:   "insensitive"  : trim; case-fold RFC-token runs in the media-type
#:                    segment before the first ";" (Content-Type style);
#:                    parameters are preserved verbatim
DEFAULT_POLICIES: Dict[str, str] = {
    "accept": "token_list",
    "accept-encoding": "token_set",
    "accept-language": "token_list",
    "authorization": "raw",
    "content-encoding": "token_set",
    "content-language": "token_list",
    "content-type": "insensitive",
    "cookie": "raw",
    "x-api-key": "raw",
}

# Bytes legal in a header *value* after RFC 7230 obs-fold is removed.
#   HTAB / SP / VCHAR / obs-text (0x80-0xFF)
_ALLOWED_VALUE_BYTES = frozenset(
    [0x09] + list(range(0x20, 0x7F)) + list(range(0x80, 0x100))
)


def _valid_value_byte(b: int) -> bool:
    return b in _ALLOWED_VALUE_BYTES


# RFC 7230 token characters (for token case-folding).
def _is_token_byte(b: int) -> bool:
    return b in (
        b"!#$%&'*+-.^_`|~0123456789"
        b"abcdefghijklmnopqrstuvwxyz"
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    )


class HeaderError(ValueError):
    """Raised when a header name/value violates the accepted wire grammar."""


class KeyTooLongError(Exception):
    """Raised by on_too_long='reject' when the canonical key exceeds the bound."""

    def __init__(self, length: int, bound: int):
        super().__init__(
            f"canonical header key is {length} bytes, bound is {bound}"
        )
        self.length = length
        self.bound = bound


@dataclass(frozen=True)
class CacheKey:
    """A constructed cache key.

    Attributes:
        key:      the final stable key string (``HK1$...`` direct,
                  ``HK1H$...`` for the hashed form of an over-long payload)
        hashed:   True iff the key is the content hash of an over-long payload
        canonical:the raw canonical bytes (length-framed), exposed for tests
                  and debugging
        length:   length of ``key`` in ASCII characters
    """

    key: str
    hashed: bool
    canonical: bytes
    length: int


HeaderInput = Union[
    Mapping[str, str],
    Mapping[bytes, bytes],
    Iterable[Tuple[str, str]],
    Iterable[Tuple[bytes, bytes]],
]


def _validate_name(name: str) -> str:
    low = name.strip().lower()
    if not low:
        raise HeaderError("empty header name")
    for ch in low:
        code = ord(ch)
        if not (0x21 <= code <= 0x7E and ch not in ' \t"(),/:;<=>?@[\\]{}'):
            raise HeaderError(f"illegal character in header name: {name!r}")
    return low


def _validate_value(raw: bytes) -> None:
    # obs-fold would already have been unfolded by a conforming parser; reject
    # bare CR/LF/NUL so attackers cannot inject framing or fold lines.
    if not set(raw) <= _ALLOWED_VALUE_BYTES:
        raise HeaderError("illegal character in header value")


def _trim_ows(b: bytes) -> bytes:
    return b.strip(b" \t")


def _split_quoted_commas(value: bytes) -> List[bytes]:
    """Split on commas that are outside quoted-strings; no trimming here."""
    if 0x22 not in value:  # fast path: no quoted-string present
        return value.split(b",")
    parts: List[bytes] = []
    buf = bytearray()
    quoted = False
    i, n = 0, len(value)
    while i < n:
        c = value[i]
        if quoted:
            buf.append(c)
            if c == 0x5C and i + 1 < n:  # backslash escape: keep next verbatim
                i += 1
                buf.append(value[i])
            elif c == 0x22:  # closing quote
                quoted = False
        else:
            if c == 0x22:
                quoted = True
                buf.append(c)
            elif c == 0x2C:  # comma
                parts.append(bytes(buf))
                buf = bytearray()
            else:
                buf.append(c)
        i += 1
    if quoted:
        raise HeaderError("unterminated quoted-string in header value")
    parts.append(bytes(buf))
    return parts


def _casefold_token_element(element: bytes) -> bytes:
    """Lowercase the leading RFC-token run; preserve the rest verbatim."""
    i, n = 0, len(element)
    while i < n and _is_token_byte(element[i]):
        i += 1
    return element[:i].lower() + element[i:]


def _normalize_elements(value: bytes, policy: str) -> Tuple[bytes, ...]:
    pieces = _split_quoted_commas(value)
    elements: List[bytes] = []
    for piece in pieces:
        trimmed = _trim_ows(piece)
        if not trimmed:
            continue  # drop empty list elements (",," / trailing comma noise)
        if policy in ("token_list", "token_set"):
            trimmed = _casefold_token_element(trimmed)
        elements.append(trimmed)
    if policy == "token_set":
        return tuple(sorted(set(elements)))
    return tuple(elements)


def _casefold_media_type(value: bytes) -> bytes:
    """For "insensitive": lowercase RFC-token runs in the media-type segment
    (everything before the first ';'), keep parameters verbatim.

    Parameter values may be case-sensitive (e.g. multipart boundary), so they
    are never case-folded; parameter *names* are kept verbatim too (a
    conservative under-normalization that can never merge distinct requests).
    """
    head, sep, tail = value.partition(b";")
    out = bytearray()
    run = bytearray()
    for byte in head:
        if _is_token_byte(byte):
            run.append(byte)
        else:
            out.extend(bytes(run).lower())
            run = bytearray()
            out.append(byte)
    out.extend(bytes(run).lower())
    return bytes(out) + sep + tail


def _netstring(b: bytes) -> bytes:
    return str(len(b)).encode("ascii") + b":" + b + b","


def _decode_headers(headers: HeaderInput) -> List[Tuple[str, bytes]]:
    if isinstance(headers, Mapping):
        items = headers.items()
    else:
        items = list(headers)
    out: List[Tuple[str, bytes]] = []
    for raw_name, raw_value in items:
        if isinstance(raw_name, bytes):
            name = raw_name.decode("ascii")
        else:
            name = raw_name
        if isinstance(raw_value, bytes):
            value = raw_value
        else:
            value = raw_value.encode("latin-1", errors="strict")
        out.append((name, value))
    return out


class KeyBuilder:
    """Reusable, pre-validated cache key builder.

    Args:
        participating: header names (case-insensitive) that take part in keys.
            Defaults to a content-negotiation allow-list.
        policies: per-header policy overrides/extensions.
        max_canonical_bytes: hard upper bound on the canonical payload.
        on_too_long: "hash" (default; BLAKE2b/256, distinct key prefix) or
            "reject" (raise KeyTooLongError).
    """

    def __init__(
        self,
        participating: Sequence[str] = DEFAULT_PARTICIPATING,
        policies: Mapping[str, str] = None,
        max_canonical_bytes: int = MAX_CANONICAL_BYTES,
        on_too_long: str = "hash",
    ) -> None:
        if on_too_long not in ("hash", "reject"):
            raise ValueError("on_too_long must be 'hash' or 'reject'")
        merged_policies = dict(DEFAULT_POLICIES)
        if policies:
            for k, v in policies.items():
                if v not in ("raw", "token_list", "token_set", "insensitive"):
                    raise ValueError(f"unknown policy {v!r} for header {k!r}")
                merged_policies[k.lower()] = v

        names = sorted({_validate_name(n) for n in participating})
        self._names: Tuple[str, ...] = tuple(names)
        self._policies: Dict[str, str] = merged_policies
        self._max_bytes = int(max_canonical_bytes)
        self._on_too_long = on_too_long

    @property
    def participating(self) -> Tuple[str, ...]:
        return self._names

    def build(self, headers: HeaderInput) -> CacheKey:
        rows = _decode_headers(headers)

        # 1. validate names/values, lowercase names, and merge repeated fields.
        merged: Dict[str, bytearray] = {}
        for raw_name, raw_value in rows:
            name = _validate_name(raw_name)
            _validate_value(raw_value)
            if name in merged:
                # RFC 7230 3.2.2: repeated field lines == comma-joined value.
                merged[name].append(0x2C)
                merged[name].extend(raw_value)
            else:
                merged[name] = bytearray(raw_value)

        # 2. canonicalize every allow-listed header in a fixed order.
        records: List[Tuple[bytes, Tuple[bytes, ...]]] = []
        for name in self._names:
            policy = self._policies.get(name, "raw")
            present = name in merged
            value = bytes(merged.get(name, b""))
            if policy == "raw":
                elements = (_trim_ows(value),) if present else ()
            elif policy == "insensitive":
                elements = (
                    (_casefold_media_type(_trim_ows(value)),)
                    if present
                    else ()
                )
            else:
                elements = _normalize_elements(value, policy)
            records.append((name.encode("ascii"), elements))

        # 3. serialize injectively:
        #       HK1, nrec, [ name-len : name, flag, nelem,
        #                    ( elem-len : elem )* ]*
        out = bytearray()
        out.extend(VERSION)
        out.append(0x2C)
        out.extend(str(len(records)).encode("ascii"))
        out.append(0x2C)
        for name_b, elements in records:
            out.extend(_netstring(name_b))
            out.append(0x31 if elements else 0x30)  # presence flag '1'/'0'
            out.append(0x2C)
            out.extend(str(len(elements)).encode("ascii"))
            out.append(0x2C)
            for element in elements:
                out.extend(_netstring(element))
        canonical = bytes(out)

        # 4. enforce the length bound explicitly — never truncate.
        if len(canonical) <= self._max_bytes:
            key = VERSION.decode("ascii") + "$" + base64.urlsafe_b64encode(
                canonical
            ).rstrip(b"=").decode("ascii")
            hashed = False
        elif self._on_too_long == "reject":
            raise KeyTooLongError(len(canonical), self._max_bytes)
        else:
            digest = hashlib.blake2b(
                VERSION + b"$overflow$" + canonical, digest_size=32
            ).digest()
            key = (
                VERSION.decode("ascii")
                + "H$"
                + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            )
            hashed = True

        return CacheKey(
            key=key,
            hashed=hashed,
            canonical=canonical,
            length=len(key),
        )


def build_cache_key(
    headers: HeaderInput,
    participating: Sequence[str] = DEFAULT_PARTICIPATING,
    policies: Mapping[str, str] = None,
    max_canonical_bytes: int = MAX_CANONICAL_BYTES,
    on_too_long: str = "hash",
) -> CacheKey:
    """One-shot wrapper around :class:`KeyBuilder`."""
    return KeyBuilder(
        participating=participating,
        policies=policies,
        max_canonical_bytes=max_canonical_bytes,
        on_too_long=on_too_long,
    ).build(headers)
