"""Typed errors for content-encoding negotiation and streaming decode.

Layer numbering convention used across the package:
    layer 1 = the FIRST coding listed in Content-Encoding, i.e. the
    outermost coding, which is the first one removed while decoding.
"""


class CodecError(Exception):
    """Base class for all errors raised by this package."""


class NegotiationError(CodecError):
    """Accept-Encoding / Content-Encoding negotiation failed."""


class HeaderParseError(CodecError):
    """A Content-Encoding or Accept-Encoding header is malformed."""


class UnknownEncodingError(CodecError):
    """A declared content coding is not supported by this decoder."""

    def __init__(self, token, layer):
        self.token = token
        self.layer = layer
        super().__init__(
            "unknown content coding %r at layer %d "
            "(layer N = Nth token in Content-Encoding)" % (token, layer)
        )


class LayerDecodeError(CodecError):
    """A decode layer failed while processing the byte stream.

    kind is one of: "header", "integrity", "trailing".
    """

    def __init__(self, layer, token, kind, detail, hint=None):
        self.layer = layer
        self.token = token
        self.kind = kind
        self.detail = detail
        self.hint = hint
        msg = "layer %d (%s) %s error: %s" % (layer, token, kind, detail)
        if hint:
            msg += "; hint: " + hint
        super().__init__(msg)


class TruncatedStreamError(CodecError):
    """The byte stream ended before a coding layer was complete."""

    def __init__(self, layer, token, detail="stream ended mid-frame"):
        self.layer = layer
        self.token = token
        super().__init__(
            "layer %d (%s) truncated: %s" % (layer, token, detail)
        )


class LengthMismatchError(CodecError):
    """Received body length does not match the declared Content-Length."""

    def __init__(self, declared, received):
        self.declared = declared
        self.received = received
        super().__init__(
            "Content-Length mismatch: declared %d bytes, received %d bytes"
            % (declared, received)
        )
