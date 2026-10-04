"""Content-Encoding negotiation and bounded-memory streaming decompression.

Standard library only. Supported encodings: identity, gzip (x-gzip),
deflate (zlib-wrapped or raw), bzip2 (x-bzip2). Unknown encodings are
rejected; the declared encoding chain is decoded layer by layer in reverse
declaration order, and order reversals are detected from stream signatures.
"""

from .chain import (
    DEFAULT_CHUNK,
    DecoderChain,
    StreamDecoder,
    decode_bytes,
    iter_decode,
    sniff_encoding,
)
from .errors import (
    ContentEncodingError,
    DecodeError,
    EncodingOrderError,
    LengthMismatchError,
    TruncatedStreamError,
    UnknownEncodingError,
)
from .negotiate import accept_encoding, parse_content_encoding

__all__ = [
    "DEFAULT_CHUNK",
    "DecoderChain",
    "StreamDecoder",
    "decode_bytes",
    "iter_decode",
    "sniff_encoding",
    "accept_encoding",
    "parse_content_encoding",
    "ContentEncodingError",
    "UnknownEncodingError",
    "EncodingOrderError",
    "DecodeError",
    "TruncatedStreamError",
    "LengthMismatchError",
]
