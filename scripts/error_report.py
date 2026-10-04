"""Generate the exception report: one entry per required failure scenario.

Usage (repo root):
    python3 scripts/error_report.py
Writes reports/exceptions.txt and prints the same report.
"""

import bz2
import gzip
import os
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from streamdecode import (  # noqa: E402
    ContentEncodingError,
    DecoderChain,
    decode_bytes,
    iter_decode,
)

PAYLOAD = (b"The quick brown fox jumps over the lazy dog. " * 4000)


def attempt(title, fn):
    try:
        fn()
    except ContentEncodingError as exc:
        line = "[REJECTED] %s\n  type   : %s\n  layer  : %s (encoding %r)\n  message: %s" % (
            title,
            type(exc).__name__,
            exc.layer,
            exc.encoding,
            exc,
        )
        return line
    except Exception as exc:  # unexpected exception class
        return "[WRONG-TYPE] %s -> %s: %s" % (title, type(exc).__name__, exc)
    return "[NOT REJECTED!] %s" % title


def run():
    lines = []
    lines.append("streamdecode exception report")
    lines.append("=" * 60)

    # 1. Unknown encoding, single layer
    lines.append(
        attempt(
            "unknown encoding 'br' (single layer)",
            lambda: DecoderChain("br"),
        )
    )
    # 2. Unknown encoding at an inner layer, correct index reported
    lines.append(
        attempt(
            "unknown encoding 'zstd' at layer 1 of 'gzip, zstd, deflate'",
            lambda: DecoderChain("gzip, zstd, deflate"),
        )
    )
    # 3. Reversed declaration order (applied gzip then deflate, header reversed)
    blob_a = zlib.compress(gzip.compress(PAYLOAD))
    lines.append(
        attempt(
            "reversed order: actual [gzip then deflate], declared 'deflate, gzip'",
            lambda: decode_bytes("deflate, gzip", blob_a),
        )
    )
    # 4. Reversed declaration order (other direction)
    blob_b = gzip.compress(zlib.compress(PAYLOAD))
    lines.append(
        attempt(
            "reversed order: actual [deflate then gzip], declared 'gzip, deflate'",
            lambda: decode_bytes("gzip, deflate", blob_b),
        )
    )
    # 5. Truncated single layer
    comp = gzip.compress(PAYLOAD)
    lines.append(
        attempt(
            "truncated gzip stream (cut at 50%%)",
            lambda: decode_bytes(["gzip"], comp[: len(comp) // 2]),
        )
    )
    # 6. Truncated outer layer of a chain
    multi = zlib.compress(gzip.compress(PAYLOAD))
    lines.append(
        attempt(
            "truncated outer 'deflate' layer in 'gzip, deflate'",
            lambda: decode_bytes("gzip, deflate", multi[: len(multi) // 2]),
        )
    )
    # 7. Truncated inner layer (outer stream complete)
    inner_cut = gzip.compress(PAYLOAD)
    inner_cut = inner_cut[: len(inner_cut) // 2]
    wrapped = zlib.compress(inner_cut)
    lines.append(
        attempt(
            "truncated inner 'gzip' layer in 'gzip, deflate' (outer valid)",
            lambda: decode_bytes("gzip, deflate", wrapped),
        )
    )
    # 8. Truncated bzip2
    bz = bz2.compress(PAYLOAD)
    lines.append(
        attempt(
            "truncated bzip2 stream (last byte removed)",
            lambda: decode_bytes(["bzip2"], bz[:-1]),
        )
    )
    # 9. Trailing garbage
    lines.append(
        attempt(
            "trailing bytes after gzip EOF",
            lambda: decode_bytes(["gzip"], comp + b"GARBAGE"),
        )
    )
    # 10. Corrupt payload that matches no signature
    junk = bytes(range(256)) * 32
    lines.append(
        attempt(
            "declared gzip but body has no gzip signature / is corrupt",
            lambda: decode_bytes(["gzip"], junk),
        )
    )
    # 11. Content-Length mismatch: too few
    lines.append(
        attempt(
            "Content-Length too large: declared %d, got %d" % (len(comp), len(comp) - 5),
            lambda: b"".join(
                iter_decode(
                    ["gzip"], [comp[:-5]], declared_length=len(comp)
                )
            ),
        )
    )
    # 12. Content-Length mismatch: too many
    lines.append(
        attempt(
            "Content-Length too small: declared %d, got more" % len(comp),
            lambda: b"".join(
                iter_decode(
                    ["gzip"],
                    [comp, b"EXTRA-BYTES"],
                    declared_length=len(comp),
                )
            ),
        )
    )
    # 13. Malformed header token
    lines.append(
        attempt(
            "malformed Content-Encoding header 'gzip, bad token'",
            lambda: DecoderChain("gzip, bad token"),
        )
    )
    return "\n\n".join(lines) + "\n"


if __name__ == "__main__":
    report = run()
    print(report)
    out_dir = os.path.join(ROOT, "reports")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "exceptions.txt"), "w") as f:
        f.write(report)
