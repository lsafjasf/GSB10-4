"""Streaming content-encoding negotiation and decoding (stdlib only)."""

from .errors import (
    CodecError,
    HeaderParseError,
    LayerDecodeError,
    LengthMismatchError,
    NegotiationError,
    TruncatedStreamError,
    UnknownEncodingError,
)
from .negotiation import (
    build_accept_header,
    negotiate,
    parse_accept_encoding,
    parse_content_encoding,
)
from .reference import decode_chain, encode_chain
from .streaming import StreamingDecoder, decode_stream

__all__ = [
    "CodecError",
    "HeaderParseError",
    "LayerDecodeError",
    "LengthMismatchError",
    "NegotiationError",
    "TruncatedStreamError",
    "UnknownEncodingError",
    "StreamingDecoder",
    "build_accept_header",
    "decode_chain",
    "decode_stream",
    "encode_chain",
    "negotiate",
    "parse_accept_encoding",
    "parse_content_encoding",
]
