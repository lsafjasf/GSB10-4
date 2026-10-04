"""Public exception hierarchy for streamdecode.

Every error that is tied to a specific Content-Encoding layer carries:
  layer    -- 0-based index into the *declared* Content-Encoding list
  encoding -- the declared encoding token at that layer
"""


class ContentEncodingError(Exception):
    def __init__(self, message, *, layer=None, encoding=None):
        self.layer = layer
        self.encoding = encoding
        if layer is not None:
            message = "content-encoding layer %d (%r): %s" % (layer, encoding, message)
        super().__init__(message)


class UnknownEncodingError(ContentEncodingError):
    """An encoding token in Content-Encoding is not supported."""


class EncodingOrderError(ContentEncodingError):
    """The byte stream does not match the declared encoding order.

    Raised when the outermost bytes of the stream carry the signature of a
    *different* supported encoding than the one the header declares as
    outermost -- the classic symptom of a reversed Content-Encoding list.
    """

    def __init__(self, declared, detected):
        self.declared = list(declared)
        self.detected = detected
        outermost = self.declared[-1]
        suggested = ", ".join(reversed(self.declared))
        message = (
            "stream starts with a %r signature, but declared Content-Encoding "
            "'%s' makes %r the outermost encoding; the declared order is "
            "likely reversed or the header is wrong (did the server mean '%s'?)"
            % (detected, ", ".join(self.declared), outermost, suggested)
        )
        super().__init__(message, layer=len(self.declared) - 1, encoding=outermost)


class DecodeError(ContentEncodingError):
    """Malformed compressed data or trailing garbage at a layer."""


class TruncatedStreamError(ContentEncodingError):
    """Input ended before a layer's compressed stream was complete."""


class LengthMismatchError(ContentEncodingError):
    """Received byte count does not match the declared Content-Length."""

    def __init__(self, expected, actual, *, at_least=False):
        self.expected = expected
        self.actual = actual
        qualifier = "at least " if at_least else ""
        super().__init__(
            "Content-Length mismatch: declared %d bytes, received %s%d bytes"
            % (expected, qualifier, actual)
        )
