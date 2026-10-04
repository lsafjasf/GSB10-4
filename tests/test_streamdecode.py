"""Self-test suite for streamdecode (standard-library unittest only).

Run from the repository root:
    python3 -m unittest discover -s tests -t . -v
"""

import bz2
import gzip
import hashlib
import os
import random
import sys
import tracemalloc
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streamdecode import (  # noqa: E402
    DecoderChain,
    DecodeError,
    EncodingOrderError,
    LengthMismatchError,
    TruncatedStreamError,
    UnknownEncodingError,
    accept_encoding,
    decode_bytes,
    iter_decode,
)
from streamdecode.negotiate import parse_content_encoding  # noqa: E402


# --------------------------------------------------------------------------
# Fixture helpers
# --------------------------------------------------------------------------

def raw_deflate(data, level=6):
    co = zlib.compressobj(level, zlib.DEFLATED, -zlib.MAX_WBITS)
    return co.compress(data) + co.flush()


def encode_layer(name, data):
    if name == "identity":
        return data
    if name == "gzip":
        return gzip.compress(data)
    if name == "deflate":
        return zlib.compress(data)
    if name == "deflate-raw":
        return raw_deflate(data)
    if name == "bzip2":
        return bz2.compress(data)
    raise KeyError(name)


def apply_chain(encodings, data):
    """Apply encodings in declared (= application) order."""
    for name in encodings:
        data = encode_layer(name, data)
    return data


def reference_decode(encodings, blob):
    """Independent one-shot oracle using the stdlib decompress functions."""
    for name in reversed(encodings):
        if name == "identity":
            pass
        elif name == "gzip":
            blob = gzip.decompress(blob)
        elif name == "deflate":
            try:
                blob = zlib.decompress(blob)
            except zlib.error:
                blob = zlib.decompress(blob, -zlib.MAX_WBITS)
        elif name == "bzip2":
            blob = bz2.decompress(blob)
        else:
            raise KeyError(name)
    return blob


def make_payload(size, seed=0, mode="mixed"):
    if size == 0:
        return b""
    if mode == "random":
        rng = random.Random(seed)
        return rng.randbytes(size)
    rng = random.Random(seed)
    block = (
        b"The quick brown fox jumps over the lazy dog. " * 64
        + rng.randbytes(4096)
    )
    out = bytearray()
    while len(out) < size:
        out += block
    return bytes(out[:size])


def chunked(data, chunk_size):
    for off in range(0, len(data), chunk_size):
        yield data[off : off + chunk_size]


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

class SingleLayerTests(unittest.TestCase):
    def test_identity_gzip_deflate_bzip2(self):
        payload = make_payload(100_000, 1)
        cases = ["identity", "gzip", "deflate", "bzip2"]
        for enc in cases:
            with self.subTest(enc=enc):
                blob = encode_layer(enc, payload)
                self.assertEqual(
                    decode_bytes([enc], blob), payload
                )

    def test_raw_deflate_variant(self):
        payload = make_payload(50_000, 2)
        blob = raw_deflate(payload)
        self.assertEqual(decode_bytes(["deflate"], blob), payload)

    def test_empty_body(self):
        for enc in ["identity", "gzip", "deflate", "bzip2"]:
            with self.subTest(enc=enc):
                blob = encode_layer(enc, b"")
                self.assertEqual(decode_bytes([enc], blob), b"")
                self.assertEqual(
                    b"".join(iter_decode([enc], chunked(blob, 1))), b""
                )


class MultiLayerTests(unittest.TestCase):
    CHAINS = [
        ["gzip", "deflate"],
        ["deflate", "gzip"],
        ["gzip", "bzip2", "deflate"],
        ["identity", "gzip", "deflate"],
        ["gzip", "identity", "deflate"],
    ]

    def test_chained_roundtrip(self):
        payload = make_payload(200_000, 3)
        for chain in self.CHAINS:
            with self.subTest(chain=chain):
                blob = apply_chain(chain, payload)
                out = b"".join(
                    iter_decode(chain, chunked(blob, 1234))
                )
                self.assertEqual(out, payload)

    def test_header_string_accepted(self):
        payload = make_payload(10_000, 4)
        blob = apply_chain(["gzip", "deflate"], payload)
        self.assertEqual(
            decode_bytes("gzip, deflate", blob), payload
        )


class ReversedOrderTests(unittest.TestCase):
    def test_reversed_list_is_rejected_direction_a(self):
        # Applied gzip then deflate -> outermost bytes are zlib ('deflate').
        payload = make_payload(50_000, 5)
        blob = zlib.compress(gzip.compress(payload))
        # Wrong header: 'deflate, gzip' declares gzip as the outermost layer.
        with self.assertRaises(EncodingOrderError) as ctx:
            decode_bytes("deflate, gzip", blob)
        self.assertEqual(ctx.exception.detected, "deflate")
        self.assertEqual(ctx.exception.encoding, "gzip")
        self.assertEqual(ctx.exception.layer, 1)

    def test_reversed_list_is_rejected_direction_b(self):
        # Applied deflate then gzip -> outermost bytes are gzip magic.
        payload = make_payload(50_000, 6)
        blob = gzip.compress(zlib.compress(payload))
        # Wrong header: 'gzip, deflate' declares deflate as outermost.
        with self.assertRaises(EncodingOrderError) as ctx:
            decode_bytes("gzip, deflate", blob)
        self.assertEqual(ctx.exception.detected, "gzip")
        self.assertEqual(ctx.exception.encoding, "deflate")

    def test_garbage_never_decodes_to_plaintext(self):
        # Declared gzip, body is not any known signature and is not gzip.
        blob = bytes(range(256)) * 32
        with self.assertRaises(DecodeError) as ctx:
            decode_bytes(["gzip"], blob)
        self.assertEqual(ctx.exception.layer, 0)
        self.assertEqual(ctx.exception.encoding, "gzip")


class UnknownEncodingTests(unittest.TestCase):
    def test_single_unknown(self):
        with self.assertRaises(UnknownEncodingError) as ctx:
            DecoderChain("br")
        self.assertEqual(ctx.exception.layer, 0)
        self.assertEqual(ctx.exception.encoding, "br")

    def test_unknown_inner_layer_indexed(self):
        with self.assertRaises(UnknownEncodingError) as ctx:
            DecoderChain("gzip, zstd, deflate")
        self.assertEqual(ctx.exception.layer, 1)
        self.assertEqual(ctx.exception.encoding, "zstd")

    def test_historical_compress_rejected(self):
        with self.assertRaises(UnknownEncodingError):
            DecoderChain(["compress"])

    def test_malformed_and_duplicate_tokens(self):
        with self.assertRaises(Exception):
            parse_content_encoding("gzip, gzip")
        with self.assertRaises(Exception):
            parse_content_encoding("gzip, bad token")


class TruncationTests(unittest.TestCase):
    def test_single_layer_truncation_points_at_layer(self):
        payload = make_payload(100_000, 7, mode="random")
        comp = gzip.compress(payload)
        for cut in (1, 2, len(comp) // 2, len(comp) - 1):
            with self.subTest(cut=cut):
                with self.assertRaises(TruncatedStreamError) as ctx:
                    b"".join(iter_decode(["gzip"], [comp[:cut]]))
                self.assertEqual(ctx.exception.layer, 0)
                self.assertEqual(ctx.exception.encoding, "gzip")

    def test_outer_layer_truncation(self):
        # Declared 'gzip, deflate': deflate is the outermost stream.
        payload = make_payload(100_000, 8)
        blob = apply_chain(["gzip", "deflate"], payload)
        with self.assertRaises(TruncatedStreamError) as ctx:
            b"".join(iter_decode("gzip, deflate", [blob[: len(blob) // 2]]))
        self.assertEqual(ctx.exception.layer, 1)
        self.assertEqual(ctx.exception.encoding, "deflate")

    def test_inner_layer_truncation(self):
        # Complete outer (deflate) stream wrapping a truncated inner gzip.
        payload = make_payload(100_000, 9, mode="random")
        inner = gzip.compress(payload)
        inner_truncated = inner[: len(inner) // 2]
        blob = zlib.compress(inner_truncated)
        with self.assertRaises(TruncatedStreamError) as ctx:
            decode_bytes("gzip, deflate", blob)
        self.assertEqual(ctx.exception.layer, 0)
        self.assertEqual(ctx.exception.encoding, "gzip")

    def test_trailing_garbage_rejected(self):
        payload = make_payload(10_000, 10)
        blob = gzip.compress(payload) + b"not part of the stream"
        with self.assertRaises(DecodeError) as ctx:
            decode_bytes(["gzip"], blob)
        self.assertEqual(ctx.exception.layer, 0)


class LengthMismatchTests(unittest.TestCase):
    def test_too_few_bytes(self):
        payload = make_payload(10_000, 11)
        blob = gzip.compress(payload)
        with self.assertRaises(LengthMismatchError) as ctx:
            b"".join(
                iter_decode(
                    ["gzip"], [blob[:-10]], declared_length=len(blob)
                )
            )
        self.assertEqual(ctx.exception.expected, len(blob))
        self.assertEqual(ctx.exception.actual, len(blob) - 10)

    def test_too_many_bytes(self):
        payload = make_payload(10_000, 12)
        blob = gzip.compress(payload)
        with self.assertRaises(LengthMismatchError):
            b"".join(
                iter_decode(
                    ["gzip"],
                    [blob, b"extra bytes"],
                    declared_length=len(blob),
                )
            )


class NegotiationTests(unittest.TestCase):
    def test_accept_encoding_lists_only_supported(self):
        header = accept_encoding()
        self.assertIn("gzip", header)
        self.assertIn("deflate", header)
        self.assertIn("bzip2", header)
        self.assertTrue(header.endswith("*;q=0"))

    def test_parse_and_alias_normalisation(self):
        self.assertEqual(parse_content_encoding("gzip, Deflate"), ["gzip", "deflate"])
        self.assertEqual(parse_content_encoding("X-Gzip"), ["gzip"])
        self.assertEqual(parse_content_encoding(None), [])
        self.assertEqual(parse_content_encoding("  "), [])


class DifferentialTests(unittest.TestCase):
    """Streaming decode vs one-shot stdlib oracle: must be byte-identical."""

    CHAINS = [
        ["gzip"],
        ["deflate"],
        ["bzip2"],
        ["gzip", "deflate"],
        ["deflate", "gzip"],
        ["bzip2", "deflate", "gzip"],
    ]
    SIZES = [0, 1, 7, 1000, 65_536, (1 << 20) + 3, (4 << 20) + 7]

    def test_byte_identical_to_one_shot_oracle(self):
        cases = 0
        for chain in self.CHAINS:
            for size in self.SIZES:
                for mode in ("mixed", "random"):
                    payload = make_payload(size, seed=size + len(str(chain)), mode=mode)
                    blob = apply_chain(chain, payload)
                    oracle = reference_decode(chain, blob)
                    self.assertEqual(oracle, payload)
                    if size <= 1000:
                        chunk_sizes = [1, 3, 7, 4096]
                    else:
                        chunk_sizes = [4096, 65_536, 1 << 20]
                    for chunk_size in chunk_sizes:
                        out = b"".join(
                            iter_decode(chain, chunked(blob, chunk_size))
                        )
                        self.assertEqual(
                            hashlib.sha256(out).digest(),
                            hashlib.sha256(oracle).digest(),
                            msg=(chain, size, chunk_size, mode),
                        )
                        cases += 1
        self.assertGreater(cases, 100)


class MemoryBoundTests(unittest.TestCase):
    def _peak(self, size, seed):
        payload = make_payload(size, seed=seed, mode="random")
        blob = gzip.compress(payload, compresslevel=1)
        del payload
        tracemalloc.start()
        produced = 0
        for piece in iter_decode(["gzip"], chunked(blob, 65_536)):
            produced += len(piece)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return produced, len(blob), peak

    def test_peak_does_not_grow_with_body(self):
        # Allow generous fixed constant: Python zlib wrappers + buffers.
        produced_s, _, peak_s = self._peak(2 << 20, seed=1)
        produced_l, _, peak_l = self._peak(32 << 20, seed=1)
        self.assertEqual(produced_s, 2 << 20)
        self.assertEqual(produced_l, 32 << 20)
        # A 16x larger body may not cost more than 1 MiB of extra peak.
        self.assertLess(
            peak_l - peak_s,
            1 << 20,
            msg="small peak=%d large peak=%d" % (peak_s, peak_l),
        )
        # Absolute cap: decoding a 32 MiB stream stays under 4 MiB of traced
        # Python memory.
        self.assertLess(peak_l, 4 << 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
