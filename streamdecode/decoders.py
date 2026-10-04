"""Per-encoding pull-based decoders (Python standard library only).

Decoder contract
----------------
feed(data): accept compressed bytes. The caller guarantees the amount of
            data passed per call is bounded (the chain feeds at most
            out_size + BZ2_FEED bytes at a time).
read(n):    return at most n decompressed bytes, or b"" when no output can
            be produced from the currently buffered input.
finish():   called once after the final feed; drain all output and raise
            _Truncated if the stream ended early.

Two private exception types are used below; the chain maps them onto the
public error hierarchy, attaching the Content-Encoding layer index.

Memory notes
------------
* gzip/deflate go through zlib.decompressobj with max_length draining
  (plus unconsumed_tail), so a decoder never materialises more than n
  bytes of output for one read().
* bz2.BZ2Decompressor has no max_length mode, so it is fed at most
  BZ2_FEED bytes per call; its transient output is therefore bounded by a
  constant (the maximum expansion of 4 KiB of bzip2 input) regardless of
  the total response size. Its fixed state is bounded by the bzip2 block
  size (<= 900 KiB block, ~a few MiB of tables), also independent of N.
"""

import bz2
import zlib

# Maximum compressed bytes pushed into BZ2Decompressor in one call.
BZ2_FEED = 4096


class _DecodeFailed(Exception):
    """Non-truncation format error (bad header, trailing data, ...)."""


class _Truncated(Exception):
    """Stream ended before logical EOF."""


class IdentityDecoder:
    name = "identity"

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        self._buf += data

    def read(self, n):
        out = bytes(self._buf[:n])
        del self._buf[: len(out)]
        return out

    def finish(self):
        return b""


class ZlibDecoder:
    """gzip (wbits=31), zlib-wrapped deflate (15) or raw deflate (-15)."""

    def __init__(self, wbits, name):
        self.name = name
        self._obj = zlib.decompressobj(wbits)
        self._pending = bytearray()

    def feed(self, data):
        if self._obj.eof:
            raise _DecodeFailed("trailing data after end of %s stream" % self.name)
        self._pending += data

    def read(self, n):
        parts = []
        need = n
        while need > 0 and self._pending:
            buf = bytes(self._pending)
            try:
                out = self._obj.decompress(buf, need)
            except zlib.error as exc:
                raise _DecodeFailed("%s decode error: %s" % (self.name, exc))
            leftover = len(self._obj.unconsumed_tail) + len(self._obj.unused_data)
            del self._pending[: len(buf) - leftover]
            parts.append(out)
            need -= len(out)
            if self._obj.eof:
                if self._pending:
                    raise _DecodeFailed(
                        "trailing data after end of %s stream" % self.name
                    )
                break
            if not out and leftover == 0:
                # No error, no output, no progress: decoder needs more input.
                break
        return b"".join(parts)

    def finish(self):
        parts = []
        while self._pending:
            piece = self.read(1 << 20)
            if not piece:
                break
            parts.append(piece)
        # flush() must precede the EOF check: with max_length draining the
        # decoder may keep the final output bytes (and the end-of-stream
        # marker) in its internal buffer.
        tail = self._obj.flush()
        if tail:
            parts.append(tail)
        if not self._obj.eof:
            raise _Truncated(
                "truncated %s stream: unexpected end of input" % self.name
            )
        return b"".join(parts)


class GzipDecoder(ZlibDecoder):
    def __init__(self):
        super().__init__(zlib.MAX_WBITS | 16, "gzip")


class DeflateDecoder:
    """HTTP 'deflate' is either a zlib-wrapped stream or raw deflate.

    The wrapper is detected from the first two bytes (valid zlib header:
    CM == 8, CINFO <= 7, (CMF*256 + FLG) % 31 == 0) before any data is
    forwarded, so streaming behaviour is unchanged after detection.
    """

    name = "deflate"

    def __init__(self):
        self._head = bytearray()
        self._inner = None

    def _select(self):
        b0, b1 = self._head[0], self._head[1]
        zlib_header = (
            (b0 & 0x0F) == 8
            and (b0 >> 4) <= 7
            and (b0 * 256 + b1) % 31 == 0
        )
        wbits = zlib.MAX_WBITS if zlib_header else -zlib.MAX_WBITS
        self._inner = ZlibDecoder(wbits, "deflate")
        self._inner.feed(bytes(self._head))
        self._head.clear()

    def feed(self, data):
        if self._inner is None:
            self._head += data
            if len(self._head) >= 2:
                self._select()
        else:
            self._inner.feed(data)

    def read(self, n):
        if self._inner is None:
            return b""
        return self._inner.read(n)

    def finish(self):
        if self._inner is None:
            # Fewer than 2 bytes cannot be a complete deflate stream.
            raise _Truncated("truncated deflate stream: unexpected end of input")
        return self._inner.finish()


class Bz2Decoder:
    name = "bzip2"

    def __init__(self):
        self._obj = bz2.BZ2Decompressor()
        self._pending = bytearray()
        self._out = bytearray()

    def feed(self, data):
        if self._obj.eof:
            raise _DecodeFailed("trailing data after end of bzip2 stream")
        self._pending += data

    def read(self, n):
        while len(self._out) < n and self._pending:
            piece = bytes(self._pending[:BZ2_FEED])
            del self._pending[: len(piece)]
            try:
                produced = self._obj.decompress(piece)
            except (OSError, EOFError) as exc:
                raise _DecodeFailed("bzip2 decode error: %s" % (exc,))
            self._out += produced
            if self._obj.eof:
                if self._pending or self._obj.unused_data:
                    raise _DecodeFailed("trailing data after end of bzip2 stream")
                break
        out = bytes(self._out[:n])
        del self._out[: len(out)]
        return out

    def finish(self):
        while self._pending:
            piece = self.read(1 << 20)
            if not piece:
                break
        if not self._obj.eof:
            raise _Truncated("truncated bzip2 stream: unexpected end of input")
        out = bytes(self._out)
        self._out.clear()
        return out


# Canonical name -> decoder factory.
REGISTRY = {
    "identity": IdentityDecoder,
    "gzip": GzipDecoder,
    "x-gzip": GzipDecoder,
    "deflate": DeflateDecoder,
    "bzip2": Bz2Decoder,
    "x-bzip2": Bz2Decoder,
}

# Aliases normalised to the canonical Content-Encoding token.
ALIASES = {
    "x-gzip": "gzip",
    "x-bzip2": "bzip2",
}
