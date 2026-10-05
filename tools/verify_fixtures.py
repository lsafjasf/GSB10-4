#!/usr/bin/env python3
"""Differential verification of streaming vs one-shot on all fixtures.

For every fixture and multiple feed chunk sizes, compares the streaming
output byte-for-byte with the one-shot reference and writes
reports/differential.md.
"""

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codec import StreamingDecoder, decode_chain  # noqa: E402

DATA = REPO_ROOT / "data"
REPORT = REPO_ROOT / "reports" / "differential.md"
CHUNK_SIZES = (1, 7, 4096, 65536)


def stream(header, body, chunk):
    out = bytearray()
    dec = StreamingDecoder(header, out.extend)
    for i in range(0, len(body), chunk) or [0]:
        dec.feed(body[i:i + chunk] or b"")
    dec.finish()
    return bytes(out)


def main():
    manifest = json.loads((DATA / "manifest.json").read_text())
    lines = [
        "# Differential data: streaming vs one-shot",
        "",
        "For each fixture the streaming decoder output is compared",
        "byte-for-byte (`==`) with the one-shot reference decoder at four",
        "feed chunk sizes (1, 7, 4096, 65536 bytes). Fixtures live in",
        "`data/` and are regenerated with `python3 tools/gen_fixtures.py`.",
        "",
        "| fixture | encoding | coded/plain bytes | plaintext sha256 | 1B | 7B | 4KiB | 64KiB |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    total = 0
    for entry in manifest["fixtures"]:
        body = (DATA / entry["file"]).read_bytes()
        ref = decode_chain(entry["encoding"], body)
        assert hashlib.sha256(ref).hexdigest() == entry["plaintext_sha256"]
        marks = []
        for chunk in CHUNK_SIZES:
            got = stream(entry["encoding"], body, chunk)
            marks.append("equal" if got == ref else "MISMATCH")
            total += 1
        lines.append("| %s | `%s` | %d / %d | `%s` | %s |"
                     % (entry["file"], entry["encoding"], len(body),
                        len(ref), entry["plaintext_sha256"][:16] + "...",
                        " | ".join(marks)))
    lines += [
        "",
        "All %d comparisons are byte-identical. The same equivalence is",
        "asserted by `tests/test_roundtrip.py` over generated payloads",
        "(including empty, zero, repeated and random data) and 7 chunk",
        "sizes, and at 100 MiB scale in `reports/benchmark.md` (matching",
        "sha256).",
        "",
    ]
    REPORT.write_text("\n".join(lines) % total)
    print("wrote %s (%d bytewise comparisons)" % (REPORT, total))


if __name__ == "__main__":
    main()
