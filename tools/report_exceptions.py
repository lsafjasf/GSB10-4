#!/usr/bin/env python3
"""Trigger every error scenario and write reports/exceptions.md.

Captures the actual exception type/message/layer so the report shows the
real diagnostics rather than aspirational ones.
"""

import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codec import (
    HeaderParseError,
    LayerDecodeError,
    LengthMismatchError,
    NegotiationError,
    StreamingDecoder,
    TruncatedStreamError,
    UnknownEncodingError,
    decode_stream,
    encode_chain,
    negotiate,
    parse_content_encoding,
)

PAYLOAD = (b"the quick brown fox jumps over the lazy dog\n" * 5000)
REPORT = REPO_ROOT / "reports" / "exceptions.md"


def trigger(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        layer = getattr(exc, "layer", "-")
        token = getattr(exc, "token", "-")
        return type(exc).__name__, str(exc), layer, token
    return "NO ERROR", "decoder accepted an invalid stream", "-", "-"


def attempt(header, body, declared=None):
    def run():
        out = bytearray()
        decode_stream(header, io.BytesIO(body), out.extend,
                      declared_length=declared)
    return trigger(run)


def construct(header):
    def run():
        StreamingDecoder(header, lambda _b: None)
    return trigger(run)


def main():
    gzip_body = encode_chain("gzip", PAYLOAD)
    deflate_body = encode_chain("deflate", PAYLOAD)
    chain_body = encode_chain("gzip, deflate", PAYLOAD)

    cases = [
        ("unknown coding, layer 1", construct("br")),
        ("unknown coding, layer 2", construct("gzip, zstd")),
        ("malformed Content-Encoding",
         trigger(lambda: parse_content_encoding("gzip,,deflate"))),
        ("406 negotiation: everything refused",
         trigger(lambda: negotiate("*;q=0"))),
        ("reversed chain A (gzip outer, declared inner)",
         attempt("gzip, deflate",
                 encode_chain("deflate, gzip", PAYLOAD))),
        ("reversed chain B (deflate outer, declared inner)",
         attempt("deflate, gzip", chain_body)),
        ("gzip truncated at 1 byte", attempt("gzip", gzip_body[:1])),
        ("gzip truncated at 10 bytes", attempt("gzip", gzip_body[:10])),
        ("gzip truncated mid-stream",
         attempt("gzip", gzip_body[: len(gzip_body) // 2])),
        ("gzip truncated at last byte",
         attempt("gzip", gzip_body[:-1])),
        ("deflate truncated",
         attempt("deflate", deflate_body[:-5])),
        ("chain truncated (outer layer 2 cut)",
         attempt("gzip, deflate", chain_body[: len(chain_body) // 2])),
        ("chain corruption (byte flipped mid-wire)",
         attempt("gzip, deflate",
                 flip(chain_body, len(chain_body) // 2))),
        ("Content-Length too large",
         attempt("gzip", gzip_body, declared=len(gzip_body) + 10)),
        ("Content-Length too small",
         attempt("gzip", gzip_body, declared=len(gzip_body) - 10)),
        ("trailing bytes after gzip frame",
         attempt("gzip", gzip_body + b"\xde\xad\xbe\xef",
                 declared=len(gzip_body) + 4)),
        ("gzip CRC trailer corrupted",
         attempt("gzip", flip(gzip_body, -5))),
        ("random garbage where gzip frame expected",
         attempt("gzip", b"\x00" * 100)),
    ]

    lines = [
        "# Exception report",
        "",
        "Each row was produced by actually triggering the condition with "
        "`tools/report_exceptions.py`.",
        "",
        "Layer N = Nth token of Content-Encoding (1 = first listed); the",
        "outermost coding is decoded first and equals the last token.",
        "",
        "| scenario | exception | layer | token | message |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, (etype, msg, layer, token) in cases:
        lines.append(
            "| %s | %s | %s | %s | %s |"
            % (name, etype, layer, token, msg.replace("|", "\\|"))
        )
    REPORT.write_text("\n".join(lines) + "\n")
    print("wrote %s (%d scenarios)" % (REPORT, len(cases)))


def flip(data, index):
    buf = bytearray(data)
    buf[index] ^= 0xFF
    return bytes(buf)


if __name__ == "__main__":
    main()
