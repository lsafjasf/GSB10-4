"""Streaming, bounded-memory content decoder (stdlib only).

Pipeline model
--------------
Per RFC 9110 8.4.1, Content-Encoding lists codings in the order they
were APPLIED, so a chain "gzip, deflate" means the body is
deflate(gzip(payload)).  Decoding removes codings in reverse
declaration order: the last token (outermost coding) is decoded first.
Each coding is one pipeline *stage*.

Memory bound
------------
* Incoming transport chunks are at most ``in_chunk`` bytes.
* Every zlib call uses ``max_length=out_chunk`` (and the flush() length
  argument), so one call yields at most ``out_chunk`` output bytes; any
  input that did not fit the output limit is re-fed from
  ``unconsumed_tail`` instead of being buffered here.
* A produced chunk is pushed into the next stage immediately; the only
  byte buffers that ever live simultaneously are one <= in_chunk input
  chunk plus one <= out_chunk chunk per pipeline stage (plus zlib's own
  fixed-size internal state: a 32 KiB LZ77 window and small headers).

Therefore peak memory is O(stages * out_chunk + in_chunk): independent of
the total response size.

Layer numbering: layer N = the Nth token of Content-Encoding (1-based
position in the header).  Stages decode from N down to 1.
"""

import zlib

from .errors import (
    HeaderParseError,
    LayerDecodeError,
    LengthMismatchError,
    TruncatedStreamError,
    UnknownEncodingError,
)
from .negotiation import parse_content_encoding

DEFAULT_IN_CHUNK = 64 * 1024
DEFAULT_OUT_CHUNK = 64 * 1024

_GZIP_MAGIC = b"\x1f\x8b"
_SUPPORTED = ("gzip", "deflate", "identity")


def _looks_like_zlib_header(data):
    if len(data) < 2:
        return True  # not enough bytes to decide; zlib will judge later
    if (data[0] & 0x0F) != 8:  # CM must be DEFLATE
        return False
    return ((data[0] << 8) | data[1]) % 31 == 0


class _Stage:
    __slots__ = ("layer", "token", "wbits", "decomp", "header_checked",
                 "trailing")

    def __init__(self, layer, token):
        self.layer = layer
        self.token = token
        self.wbits = 31 if token == "gzip" else 15
        self.decomp = None if token == "identity" else zlib.decompressobj(
            self.wbits
        )
        self.header_checked = False
        # Bytes seen after the frame's end.  Counted, never buffered, so
        # arbitrarily large trailing junk cannot grow memory.
        self.trailing = 0


class StreamingDecoder:
    """Decode a coded body as bytes arrive.

    Parameters
    ----------
    header_value:
        Raw Content-Encoding header value, or a list of tokens.
    sink:
        Callable invoked with every decoded byte chunk (each <=
        out_chunk).  Use this to write straight to disk / downstream;
        never accumulate its argument if you want the memory bound.
    """

    def __init__(
        self,
        header_value,
        sink,
        in_chunk=DEFAULT_IN_CHUNK,
        out_chunk=DEFAULT_OUT_CHUNK,
    ):
        tokens = parse_content_encoding(header_value) if isinstance(
            header_value, str
        ) else list(header_value or [])
        for index, token in enumerate(tokens, start=1):
            if token not in _SUPPORTED:
                raise UnknownEncodingError(token, index)
        self._tokens = tokens
        # Decode in reverse declaration order; the stage's layer number
        # is the token's 1-based position in the header.
        self._stages = [
            _Stage(i + 1, tokens[i]) for i in range(len(tokens) - 1, -1, -1)
        ]
        self._sink = sink
        self._in_chunk = in_chunk
        self._out_chunk = out_chunk
        self._finished = False

    # ---- public API -----------------------------------------------------

    def feed(self, data):
        """Hand one transport chunk to the decoder."""
        if self._finished:
            raise RuntimeError("decoder already finished")
        if not self._stages:
            if data:
                self._sink(data)
            return
        if data:
            self._push_into_stage(0, data)

    def finish(self):
        """Signal end of stream; flush all stages and verify framing."""
        if self._finished:
            raise RuntimeError("decoder already finished")
        self._finished = True
        for idx, stage in enumerate(self._stages):
            if stage.decomp is not None:
                self._flush_stage(idx, stage)

    # ---- internals ------------------------------------------------------

    def _check_header(self, stage, head):
        """Validate leading magic bytes; catch reversed coding order."""
        token = stage.token
        if token == "gzip":
            if len(head) >= 2 and not head.startswith(_GZIP_MAGIC):
                hint = None
                if _looks_like_zlib_header(head):
                    hint = (
                        "bytes are a zlib (deflate) stream, not gzip; the "
                        "coding chain order is probably reversed. Codings "
                        "must be removed in reverse declaration order "
                        "(last Content-Encoding token first)"
                    )
                raise LayerDecodeError(
                    stage.layer, token, "header",
                    "bad gzip magic bytes %r" % head[:2], hint,
                )
        elif token == "deflate":
            if not _looks_like_zlib_header(head):
                hint = None
                if head.startswith(_GZIP_MAGIC):
                    hint = (
                        "bytes are a gzip stream, not a zlib stream; the "
                        "coding chain order is probably reversed. Codings "
                        "must be removed in reverse declaration order "
                        "(last Content-Encoding token first)"
                    )
                raise LayerDecodeError(
                    stage.layer, token, "header",
                    "bad zlib header bytes %r" % head[:2], hint,
                )

    def _push_into_stage(self, idx, data):
        stage = self._stages[idx]
        decomp = stage.decomp
        if decomp is None:  # identity
            self._route(idx, data)
            return

        if not stage.header_checked:
            self._check_header(stage, data[:2])
            stage.header_checked = True

        out_chunk = self._out_chunk
        # Re-feed any tail zlib could not consume because the output
        # limit was hit.  At most one out_chunk-sized result exists at a
        # time, so this never buffers in proportion to total size.
        while data:
            if decomp.eof:
                # Frame already complete: zlib never consumes input after
                # eof (and unused_data would accumulate it), so count any
                # further bytes as trailing garbage ourselves.
                stage.trailing += len(data)
                break
            try:
                out = decomp.decompress(data, max_length=out_chunk)
            except zlib.error as exc:
                raise self._classify(exc, stage) from None
            if out:
                self._route(idx, out)
            if decomp.eof:
                # zlib reports the same leftover bytes in both
                # unconsumed_tail and unused_data; count them once.
                stage.trailing += max(
                    len(decomp.unconsumed_tail), len(decomp.unused_data)
                )
                break
            tail = decomp.unconsumed_tail
            if not tail:
                break
            if not out and tail == data:
                raise LayerDecodeError(
                    stage.layer, stage.token, "integrity",
                    "decoder made no progress on the input stream",
                )
            data = tail

    def _flush_stage(self, idx, stage):
        decomp = stage.decomp
        out_chunk = self._out_chunk
        while True:
            try:
                out = decomp.flush(out_chunk)
            except zlib.error as exc:
                raise self._classify(exc, stage) from None
            if out:
                self._route(idx, out)
            if len(out) < out_chunk:
                break

        if not decomp.eof:
            raise TruncatedStreamError(
                stage.layer, stage.token,
                "end of input reached before the %s stream was complete"
                % stage.token,
            )
        if stage.trailing:
            raise LayerDecodeError(
                stage.layer, stage.token, "trailing",
                "%d unexpected byte(s) after the %s frame; the declared "
                "length/chain does not match the actual framing"
                % (stage.trailing, stage.token),
            )

    def _route(self, idx, chunk):
        if idx + 1 < len(self._stages):
            self._push_into_stage(idx + 1, chunk)
        else:
            self._sink(chunk)

    @staticmethod
    def _classify(exc, stage):
        text = str(exc)
        lowered = text.lower()
        if "incorrect data check" in lowered or "checksum" in lowered:
            return LayerDecodeError(
                stage.layer, stage.token, "integrity",
                "CRC/checksum failure: %s" % text,
            )
        if "incorrect length check" in lowered:
            return LayerDecodeError(
                stage.layer, stage.token, "integrity",
                "ISIZE mismatch: %s" % text,
            )
        if "incorrect header check" in lowered:
            return LayerDecodeError(
                stage.layer, stage.token, "header", "bad header: %s" % text
            )
        if (
            "incomplete" in lowered
            or "truncated" in lowered
            or "need data" in lowered
        ):
            return TruncatedStreamError(stage.layer, stage.token, text)
        return LayerDecodeError(
            stage.layer, stage.token, "integrity",
            "corrupt stream: %s" % text,
        )


def iter_body_with_content_length(readable, declared, chunk=DEFAULT_IN_CHUNK):
    """Yield body chunks enforcing the declared Content-Length.

    Raises LengthMismatchError when the stream is shorter or longer than
    declared.  This is the transport-length check; coding-level framing
    is validated by StreamingDecoder.
    """
    if declared is None:
        while True:
            block = readable.read(chunk)
            if not block:
                return
            yield block
        return

    total = 0
    while True:
        block = readable.read(chunk)
        if not block:
            break
        total += len(block)
        if total > declared:
            raise LengthMismatchError(declared, total)
        yield block
    if total != declared:
        raise LengthMismatchError(declared, total)


def decode_stream(
    header_value,
    readable,
    sink,
    declared_length=None,
    in_chunk=DEFAULT_IN_CHUNK,
    out_chunk=DEFAULT_OUT_CHUNK,
):
    """Read a coded stream to completion and decode it through ``sink``."""
    decoder = StreamingDecoder(
        header_value, sink, in_chunk=in_chunk, out_chunk=out_chunk
    )
    for block in iter_body_with_content_length(
        readable, declared_length, chunk=in_chunk
    ):
        decoder.feed(block)
    decoder.finish()
