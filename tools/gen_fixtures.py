#!/usr/bin/env python3
"""Regenerate the checked-in differential fixtures under data/.

Deterministic: same payloads, same bytes, same manifest every run.
"""

import hashlib
import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codec import encode_chain  # noqa: E402

DATA = REPO_ROOT / "data"


def payloads():
    rng = random.Random(20261005)
    text = ("GSB10-4 streaming codec fixture\n"
            "line %04d: the quick brown fox jumps over the lazy dog\n")
    text = "".join(text % i for i in range(400)).encode()
    return [
        ("text.txt", text),
        ("random.bin", rng.randbytes(65536)),
        ("zeros.bin", b"\x00" * 32768),
    ]


def main():
    DATA.mkdir(exist_ok=True)
    fixtures = []
    for name, plain in payloads():
        digest = hashlib.sha256(plain).hexdigest()
        for chain in ("gzip", "deflate", "gzip, deflate"):
            body = encode_chain(chain, plain)
            fname = "%s.%s" % (name, chain.replace(", ", "+"))
            (DATA / fname).write_bytes(body)
            fixtures.append({
                "file": fname,
                "encoding": chain,
                "plaintext_sha256": digest,
                "plaintext_size": len(plain),
                "body_size": len(body),
            })
    manifest = {"seed": 20261005, "fixtures": fixtures}
    (DATA / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("wrote %d fixtures + manifest.json to %s" % (len(fixtures), DATA))


if __name__ == "__main__":
    main()
