"""Streaming multi-layer decoding chain.

The Content-Encoding header lists encodings in application order, e.g.
``Content-Encoding: gzip, deflate`` means: encode with gzip, then with
deflate. Decoding must therefore happen in *reverse* declaration order
(deflate first, gzip second).
"""

from .decoders import REGISTRY, _DecodeFailed, _Truncated
from .errors import (
    DecodeError,
    EncodingOrderError,
    LengthMismatchError,
    TruncatedStreamError,
    UnknownEncodingError,
)
from .negotiate import canonical, parse_content_encoding

DEFAULT_CHUNK = 65536


def _looks_like_zlib_header(prefix):
    b0, b1 = prefix[0], prefix[1]
    return (
        (b0 & 0x0F) == 8
        and (b0 >> 4) <= 7
        and (b0 * 256 + b1) % 31 == 0
    )


def sniff_encoding(prefix):
    """Return the canonical encoding suggested by the stream's first bytes."""
    if prefix[:3] == b"\x1f\x8b\x08":
        return "gzip"
    if prefix[:3] == b"BZh":
        return "bzip2"
    if len(prefix) >= 2 and _looks_like_zlib_header(prefix):
        return "deflate"
    return None


class DecoderChain:
    """Builds per-layer decoders from a declared encoding list.

    ``layers`` is stored in *decode* order (outermost encoding first);
    every entry pairs the decoder with the encoding's index in the declared
    Content-Encoding list, so error reports point at the exact layer.
    """

    def __init__(self, encodings):
        if isinstance(encodings, str):
            encodings = parse_content_encoding(encodings)
        if not encodings:
            encodings = ["identity"]
        self.declared = [canonical(name) for name in encodings]
        self.layers = []
        for declared_index in range(len(self.declared) - 1, -1, -1):
            name = self.declared[declared_index]
            factory = REGISTRY.get(name)
            if factory is None:
                raise UnknownEncodingError(
                    "unknown or unsupported content encoding %r" % name,
                    layer=declared_index,
                    encoding=name,
                )
            self.layers.append((declared_index, factory()))


class StreamDecoder:
    """Pulls compressed chunks from an iterator and yields plaintext chunks.

    Per yielded piece at most ``out_size`` output bytes are alive, and each
    layer's decoder buffers a constant-size amount of data (see
    decoders.py), so total memory does not grow with the response size.
    """

    def __init__(
        self,
        encodings,
        *,
        out_size=DEFAULT_CHUNK,
        declared_length=None,
        sniff=True,
    ):
        self.chain = (
            encodings if isinstance(encodings, DecoderChain) else DecoderChain(encodings)
        )
        self.out_size = out_size
        self.declared_length = declared_length
        self.received = 0
        self._peek = bytearray()
        self._sniff_done = not sniff
        self._finished = False

    def _check_order(self):
        self._sniff_done = True
        detected = sniff_encoding(bytes(self._peek))
        if detected is None:
            return
        expected = self.chain.declared[-1]
        if canonical(expected) != canonical(detected):
            raise EncodingOrderError(self.chain.declared, detected)

    def feed(self, data):
        if self._finished:
            raise RuntimeError("feed() called after finish()")
        if not data:
            return
        self.received += len(data)
        if (
            self.declared_length is not None
            and self.received > self.declared_length
        ):
            raise LengthMismatchError(
                self.declared_length, self.received, at_least=True
            )
        if not self._sniff_done:
            self._peek += data[: 8 - len(self._peek)]
            if len(self._peek) >= 8:
                self._check_order()
        self.chain.layers[0][1].feed(data)
        yield from self._pump()

    def _wrap_decode(self, exc, declared_index):
        name = self.chain.declared[declared_index]
        raise DecodeError(str(exc), layer=declared_index, encoding=name) from None

    def _pump(self):
        layers = self.chain.layers
        while True:
            progressed = False
            for i, (declared_index, decoder) in enumerate(layers):
                try:
                    piece = decoder.read(self.out_size)
                except _DecodeFailed as exc:
                    self._wrap_decode(exc, declared_index)
                if not piece:
                    continue
                progressed = True
                if i + 1 < len(layers):
                    nxt_index, nxt = layers[i + 1]
                    try:
                        nxt.feed(piece)
                    except _DecodeFailed as exc:
                        self._wrap_decode(exc, nxt_index)
                else:
                    yield piece
            if not progressed:
                return

    def finish(self):
        if self._finished:
            return
        self._finished = True
        if (
            self.declared_length is not None
            and self.received != self.declared_length
        ):
            raise LengthMismatchError(self.declared_length, self.received)
        if not self._sniff_done:
            self._check_order()

        layers = self.chain.layers
        for i, (declared_index, decoder) in enumerate(layers):
            name = self.chain.declared[declared_index]
            try:
                tail = decoder.finish()
            except _Truncated as exc:
                raise TruncatedStreamError(
                    str(exc), layer=declared_index, encoding=name
                ) from None
            except _DecodeFailed as exc:
                raise DecodeError(
                    str(exc), layer=declared_index, encoding=name
                ) from None
            if tail:
                if i + 1 < len(layers):
                    nxt_index, nxt = layers[i + 1]
                    try:
                        nxt.feed(tail)
                    except _DecodeFailed as exc:
                        raise DecodeError(
                            str(exc),
                            layer=nxt_index,
                            encoding=self.chain.declared[nxt_index],
                        ) from None
                else:
                    for off in range(0, len(tail), self.out_size):
                        yield tail[off : off + self.out_size]
            yield from self._pump()


def iter_decode(encodings, chunks, **kwargs):
    """Decode ``chunks`` (an iterable of bytes) for a declared encoding list.

    ``encodings`` may be a list in declaration order or a raw
    Content-Encoding header string. Yields plaintext chunks of at most
    ``out_size`` bytes.
    """
    decoder = StreamDecoder(encodings, **kwargs)
    for chunk in chunks:
        yield from decoder.feed(chunk)
    yield from decoder.finish()


def decode_bytes(encodings, data, **kwargs):
    """Convenience: decode a complete bytes object, returning bytes."""
    return b"".join(iter_decode(encodings, [data], **kwargs))
