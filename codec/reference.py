"""One-shot encode/decode reference implementations (stdlib only).

These are the "ground truth" used by the differential tests: the
streaming decoder must reproduce their output byte-for-byte.

Chain semantics (RFC 9110 8.4.1): Content-Encoding lists codings in the
order they were APPLIED while encoding.  To encode with chain
"gzip, deflate" we do: deflate(gzip(data)) -- gzip applied first.
To decode, codings are removed in reverse declaration order: the LAST
token (outermost coding) is removed first.
"""

import gzip as _gzip
import zlib

from .errors import UnknownEncodingError
from .negotiation import parse_content_encoding


def encode_layer(token, data, level=6):
    """Encode one layer (single coding)."""
    if token == "gzip":
        return _gzip.compress(data, compresslevel=level, mtime=0)
    if token == "deflate":
        # RFC 9110 8.4.1.2: the "deflate" coding is a zlib stream.
        compressor = zlib.compressobj(level, zlib.DEFLATED, 15)
        return compressor.compress(data) + compressor.flush()
    if token == "identity":
        return data
    raise UnknownEncodingError(token, 0)


def decode_layer(token, data):
    """Decode one layer (single coding), strict one-shot version."""
    if token == "gzip":
        try:
            result = zlib.decompress(data, wbits=31)
        except zlib.error as exc:
            raise ValueError("gzip: %s" % exc) from exc
    elif token == "deflate":
        try:
            result = zlib.decompress(data, wbits=15)
        except zlib.error as exc:
            raise ValueError("deflate: %s" % exc) from exc
    elif token == "identity":
        result = data
    else:
        raise UnknownEncodingError(token, 0)
    return result


def encode_chain(header_value, data, level=6):
    """Apply codings in declaration order: outer = last applied.

    A response declaring "gzip, deflate" was encoded as
    gzip(deflate(data)), so application order is declaration order.
    """
    tokens = parse_content_encoding(header_value) if isinstance(
        header_value, str
    ) else list(header_value)
    for token in tokens:
        data = encode_layer(token, data, level=level)
    return data


def decode_chain(header_value, data):
    """Decode codings in reverse declaration order (outermost first).

    Layer numbers in errors refer to the token's 1-based position in the
    Content-Encoding header.
    """
    tokens = parse_content_encoding(header_value) if isinstance(
        header_value, str
    ) else list(header_value)
    for index in range(len(tokens), 0, -1):
        token = tokens[index - 1]
        if token not in ("gzip", "deflate", "identity"):
            raise UnknownEncodingError(token, index)
        data = decode_layer(token, data)
    return data
