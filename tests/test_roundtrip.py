"""Differential tests: streaming decoder vs one-shot reference.

For every (payload, chain, chunk-size) combination the streaming output
must be byte-identical both to the original payload and to the result of
the one-shot reference decoder.  Also covers the checked-in fixtures.
"""

import io
import json
import os
import random
import unittest
from pathlib import Path

from codec import StreamingDecoder, decode_chain, decode_stream, encode_chain
from codec.reference import decode_layer, encode_layer

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"

CHAINS = [
    "gzip",
    "deflate",
    "identity",
    "gzip, deflate",
    "gzip, identity",
    "x-gzip",
]

CHUNK_SIZES = [1, 3, 7, 64, 4096, 65536, 1 << 20]


def _payloads():
    rng = random.Random(20261005)
    items = [
        b"",
        b"a",
        b"ab",
        b"hello\n",
        ("utf-8 \u4e2d\u6587 \u2603\n" * 50).encode(),
        bytes(range(256)),
        b"\x00" * 10000,
        (b"line %08d: the quick brown fox\n" % 0) * 3000,
        rng.randbytes(50000),
        rng.randbytes(129971),
    ]
    return items


def stream_decode(header, body, chunk_size):
    out = bytearray()
    dec = StreamingDecoder(header, out.extend)
    if chunk_size >= len(body):
        dec.feed(body)
    else:
        for i in range(0, len(body), chunk_size):
            dec.feed(body[i:i + chunk_size])
    dec.finish()
    return bytes(out)


class DifferentialTests(unittest.TestCase):
    def test_all_combinations(self):
        failures = []
        count = 0
        for payload in _payloads():
            for chain in CHAINS:
                if not payload and "identity" in chain and len(
                    chain.split(",")
                ) > 1:
                    # empty body with a compressed layer still works, but
                    # identity-only is tested separately below
                    pass
                body = encode_chain(chain, payload)
                one_shot = decode_chain(chain, body)
                self.assertEqual(one_shot, payload, chain)
                for cs in CHUNK_SIZES:
                    got = stream_decode(chain, body, cs)
                    count += 1
                    if got != payload:
                        failures.append((chain, cs, len(payload)))
        self.assertEqual(failures, [])
        self.assertGreater(count, 50)

    def test_empty_body(self):
        # identity permits an empty body; gzip/deflate empty streams are
        # valid frames too and must decode to b"".
        for chain in ("identity", "gzip", "deflate", "gzip, deflate"):
            body = encode_chain(chain, b"")
            self.assertEqual(stream_decode(chain, body, 64), b"")

    def test_single_layer_helpers(self):
        rng = random.Random(7)
        data = rng.randbytes(4096)
        for token in ("gzip", "deflate", "identity"):
            self.assertEqual(decode_layer(token, encode_layer(token, data)),
                             data)

    def test_stream_api_with_file_and_sink(self):
        payload = b"sink me " * 10000
        body = encode_chain("gzip, deflate", payload)
        sink = io.BytesIO()
        decode_stream("gzip, deflate", io.BytesIO(body), sink.write)
        self.assertEqual(sink.getvalue(), payload)

    def test_checked_in_fixtures(self):
        manifest_path = DATA_DIR / "manifest.json"
        if not manifest_path.exists():
            self.skipTest("run tools/gen_fixtures.py first")
        manifest = json.loads(manifest_path.read_text())
        checked = 0
        for entry in manifest["fixtures"]:
            path = DATA_DIR / entry["file"]
            import hashlib
            body = path.read_bytes()
            one_shot = decode_chain(entry["encoding"], body)
            self.assertEqual(
                hashlib.sha256(one_shot).hexdigest(),
                entry["plaintext_sha256"],
                entry["file"],
            )
            for cs in (1, 37, 65536, len(body) or 1):
                got = stream_decode(entry["encoding"], body, cs)
                self.assertEqual(got, one_shot, (entry["file"], cs))
                checked += 1
        self.assertGreaterEqual(checked, 8)


if __name__ == "__main__":
    unittest.main()
