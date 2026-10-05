"""Error-path tests: every failure must be typed and carry its layer."""

import io
import unittest

from codec import (
    HeaderParseError,
    LayerDecodeError,
    LengthMismatchError,
    StreamingDecoder,
    TruncatedStreamError,
    UnknownEncodingError,
    decode_stream,
    encode_chain,
)
from codec.negotiation import parse_content_encoding

PAYLOAD = (b"the quick brown fox jumps over the lazy dog\n" * 2000)


def attempt(header, body, declared=None):
    """Decode, returning (emitted_bytes, exception_or_None)."""
    out = bytearray()
    try:
        decode_stream(header, io.BytesIO(body), out.extend,
                      declared_length=declared)
        return bytes(out), None
    except Exception as exc:  # noqa: BLE001 - tests want the object
        return bytes(out), exc


class UnknownCodingTests(unittest.TestCase):
    def test_unknown_single(self):
        with self.assertRaises(UnknownEncodingError) as ctx:
            StreamingDecoder("br", lambda b: None)
        self.assertEqual(ctx.exception.layer, 1)
        self.assertEqual(ctx.exception.token, "br")

    def test_unknown_second_layer(self):
        with self.assertRaises(UnknownEncodingError) as ctx:
            StreamingDecoder("gzip, zstd", lambda b: None)
        self.assertEqual(ctx.exception.layer, 2)
        self.assertEqual(ctx.exception.token, "zstd")

    def test_malformed_header(self):
        with self.assertRaises(HeaderParseError):
            parse_content_encoding("gzip,,deflate")


class ReversedOrderTests(unittest.TestCase):
    """A chain decoded in the wrong order must fail loudly, not emit
    garbage.  Both directions are checked, and zero bytes may be emitted
    before the error because the magic-byte check runs first."""

    def test_gzip_outer_but_declared_inner(self):
        # wire = gzip(deflate(p)) but header claims "gzip, deflate"
        wire = encode_chain("deflate, gzip", PAYLOAD)
        emitted, exc = attempt("gzip, deflate", wire)
        self.assertIsInstance(exc, LayerDecodeError)
        self.assertEqual(exc.layer, 2)          # 2nd declared token
        self.assertEqual(exc.token, "deflate")
        self.assertEqual(exc.kind, "header")
        self.assertIn("reversed", exc.hint)
        self.assertEqual(emitted, b"")          # no garbage

    def test_deflate_outer_but_declared_inner(self):
        # wire = deflate(gzip(p)) but header claims "deflate, gzip"
        wire = encode_chain("gzip, deflate", PAYLOAD)
        emitted, exc = attempt("deflate, gzip", wire)
        self.assertIsInstance(exc, LayerDecodeError)
        self.assertEqual(exc.layer, 2)
        self.assertEqual(exc.token, "gzip")
        self.assertEqual(exc.kind, "header")
        self.assertIn("reversed", exc.hint)
        self.assertEqual(emitted, b"")

    def test_correct_order_decodes(self):
        wire = encode_chain("gzip, deflate", PAYLOAD)
        emitted, exc = attempt("gzip, deflate", wire)
        self.assertIsNone(exc)
        self.assertEqual(emitted, PAYLOAD)


class TruncationTests(unittest.TestCase):
    def test_truncated_gzip(self):
        body = encode_chain("gzip", PAYLOAD)
        for cut in (1, 5, len(body) // 2, len(body) - 1, len(body) - 8):
            _, exc = attempt("gzip", body[:cut])
            self.assertIsInstance(exc, TruncatedStreamError, cut)
            self.assertEqual(exc.layer, 1)

    def test_truncated_deflate(self):
        body = encode_chain("deflate", PAYLOAD)
        _, exc = attempt("deflate", body[: len(body) - 3])
        self.assertIsInstance(exc, TruncatedStreamError)
        self.assertEqual(exc.layer, 1)

    def test_truncated_outer_of_chain(self):
        body = encode_chain("gzip, deflate", PAYLOAD)
        _, exc = attempt("gzip, deflate", body[: len(body) // 2])
        self.assertIsInstance(exc, TruncatedStreamError)
        self.assertEqual(exc.layer, 2)   # outermost = last declared token

    def test_truncated_inner_of_chain(self):
        # Cut bytes out of the *inner* gzip stream: corrupt the middle of
        # the wire so the inner layer sees a broken stream.
        body = bytearray(encode_chain("gzip, deflate", PAYLOAD))
        mid = len(body) // 2
        body[mid] ^= 0xFF
        _, exc = attempt("gzip, deflate", bytes(body))
        self.assertIsNotNone(exc)
        self.assertIn(exc.layer, (1, 2))
        self.assertIsInstance(exc, (LayerDecodeError, TruncatedStreamError))


class LengthTests(unittest.TestCase):
    def test_declared_too_large(self):
        body = encode_chain("gzip", PAYLOAD)
        _, exc = attempt("gzip", body, declared=len(body) + 10)
        self.assertIsInstance(exc, LengthMismatchError)
        self.assertEqual(exc.declared, len(body) + 10)
        self.assertEqual(exc.received, len(body))

    def test_declared_too_small(self):
        body = encode_chain("gzip", PAYLOAD)
        _, exc = attempt("gzip", body, declared=len(body) - 10)
        self.assertIsInstance(exc, LengthMismatchError)

    def test_trailing_garbage(self):
        body = encode_chain("gzip", PAYLOAD)
        extra = body + b"\xde\xad\xbe\xef"
        _, exc = attempt("gzip", extra, declared=len(extra))
        self.assertIsInstance(exc, LayerDecodeError)
        self.assertEqual(exc.kind, "trailing")
        self.assertEqual(exc.layer, 1)


class IntegrityTests(unittest.TestCase):
    def test_crc_corruption(self):
        body = bytearray(encode_chain("gzip", PAYLOAD))
        body[-5] ^= 0xFF  # inside CRC32/ISIZE trailer
        _, exc = attempt("gzip", bytes(body))
        self.assertIsInstance(exc, LayerDecodeError)
        self.assertEqual(exc.kind, "integrity")
        self.assertEqual(exc.layer, 1)

    def test_random_garbage_body(self):
        _, exc = attempt("gzip", b"\x00" * 100)
        self.assertIsInstance(exc, LayerDecodeError)
        self.assertEqual(exc.kind, "header")


if __name__ == "__main__":
    unittest.main()
