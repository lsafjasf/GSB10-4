"""Content-Encoding negotiation helpers.

parse_content_encoding parses a response Content-Encoding header into an
ordered list of canonical encoding names (declaration order = the order in
which the encodings were applied; decoders must therefore run in reverse
list order).

accept_encoding builds the client's Accept-Encoding header from only the
encodings this library can actually decode.
"""

import re

from .errors import ContentEncodingError
from .decoders import ALIASES

_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# Encodings offered by this client, highest preference first.
OFFERED = ("gzip", "deflate", "bzip2")


def canonical(name):
    return ALIASES.get(name, name)


def parse_content_encoding(header):
    """Parse a Content-Encoding header value into a canonical token list."""
    if header is None or not header.strip():
        return []
    raw_tokens = header.split(",")
    result = []
    for index, raw in enumerate(raw_tokens):
        token = raw.strip()
        if not token or ";" in token:
            raise ContentEncodingError(
                "malformed Content-Encoding token %r" % raw,
                layer=index,
                encoding=raw.strip() or None,
            )
        token = token.lower()
        if not _TOKEN_RE.match(token):
            raise ContentEncodingError(
                "malformed Content-Encoding token %r" % raw,
                layer=index,
                encoding=token,
            )
        token = canonical(token)
        if token in result:
            raise ContentEncodingError(
                "duplicate content encoding %r" % token,
                layer=index,
                encoding=token,
            )
        result.append(token)
    return result


def accept_encoding(encodings=OFFERED, *, identity=True, star_zero=True):
    """Build an Accept-Encoding header value for the supported encodings."""
    parts = list(encodings)
    if identity:
        parts.append("identity;q=0.5")
    if star_zero:
        parts.append("*;q=0")
    return ", ".join(parts)
