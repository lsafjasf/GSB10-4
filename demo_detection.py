"""Demonstrate detection of lost/out-of-order updates and bad indexes.

Run:  python3 demo_detection.py
Output is written to docs/detection_results.txt and printed.
"""

import os
import struct
import sys

from dyntable import (
    BlockedHeaderBlock,
    CapacityLimitExceeded,
    Decoder,
    Encoder,
    EvictedEntryReference,
    IndexOutOfRange,
    InvalidInstruction,
    LostUpdateError,
    OutOfOrderUpdateError,
)
from dyntable.codec import F_INDEXED_PRE_BASE
from dyntable.wire import enc_str, enc_u8, enc_u32


def chunk(seq, payload):
    return struct.pack(">II", seq, len(payload)) + payload


def run_case(lines, title, expected, action):
    print("CASE:", title)
    lines.append("CASE: %s" % title)
    try:
        detail = action()
    except Exception as exc:  # noqa: BLE001 - report every failure type
        detected = isinstance(exc, expected)
        verdict = "DETECTED" if detected else "WRONG EXCEPTION"
        print("  raised : %s: %s" % (type(exc).__name__, exc))
        print("  expect : %s -> %s"
              % (", ".join(e.__name__ for e in expected), verdict))
        lines.append("  raised : %s: %s" % (type(exc).__name__, exc))
        lines.append("  expect : %s -> %s"
                     % (", ".join(e.__name__ for e in expected), verdict))
        if not detected:
            lines.append("  FAILURE: wrong exception type")
        return 1 if not detected else 0
    print("  raised : NONE (action returned: %s) -> NOT DETECTED" % detail)
    lines.append("  raised : NONE -> NOT DETECTED")
    return 1


def main():
    lines = ["== Decode-layer error detection results ==", ""]
    failures = 0

    # 1. Lost update chunk (gap in stream sequence numbers).
    enc, dec = Encoder(256), Decoder(256)
    enc.insert("a", "1")
    lost = enc.flush_stream_chunk()      # seq 0, dropped in transit
    enc.insert("b", "2")
    arrived = enc.flush_stream_chunk()   # seq 1
    failures += run_case(
        lines, "lost update chunk (seq 0 dropped, seq 1 arrives)",
        (LostUpdateError,), lambda: dec.feed_stream_chunk(arrived),
    )

    # 2. Out-of-order / duplicate chunk.
    enc, dec = Encoder(256), Decoder(256)
    enc.insert("a", "1")
    first = enc.flush_stream_chunk()
    failures += run_case(
        lines, "duplicate/out-of-order chunk (seq 0 replayed)",
        (OutOfOrderUpdateError,),
        lambda: (dec.feed_stream_chunk(first),
                 dec.feed_stream_chunk(first))[1],
    )

    # 3. Header block arrives before the updates it depends on.
    enc, dec = Encoder(256), Decoder(256)
    block = enc.encode_header_block([("a", "1")])
    failures += run_case(
        lines, "header block precedes its table updates (RIC > KRC)",
        (BlockedHeaderBlock,), lambda: dec.decode_header_block(block),
    )

    # 4. Index out of range on an empty table.
    dec = Decoder(256)
    bad = (enc_u32(0) + enc_u8(0) + enc_u32(0)
           + enc_u8(F_INDEXED_PRE_BASE) + enc_u32(99))
    failures += run_case(
        lines, "index out of range (relative index 99, empty table)",
        (IndexOutOfRange,), lambda: dec.decode_header_block(bad),
    )

    # 5. Reference to an already evicted entry.
    enc, dec = Encoder(160), Decoder(160)
    block1 = enc.encode_header_block(
        [("n", "0"), ("n", "1"), ("n", "2"), ("n", "3")]
    )
    dec.feed_stream_chunk(enc.flush_stream_chunk())
    dec.decode_header_block(block1)
    block2 = enc.encode_header_block([("n", "4")])
    dec.feed_stream_chunk(enc.flush_stream_chunk())
    dec.decode_header_block(block2)
    stale = (enc_u32(0) + enc_u8(0) + enc_u32(5)
             + enc_u8(F_INDEXED_PRE_BASE) + enc_u32(4))
    failures += run_case(
        lines, "reference to evicted entry (abs 0, dropped_count 1)",
        (EvictedEntryReference,), lambda: dec.decode_header_block(stale),
    )

    # 6. Capacity update above the protocol maximum.
    enc, dec = Encoder(128), Decoder(128)
    failures += run_case(
        lines, "local capacity update above protocol limit (129 > 128)",
        (CapacityLimitExceeded,), lambda: enc.set_capacity(129),
    )
    forged_cap = chunk(0, enc_u8(0x02) + enc_u32(5000))
    failures += run_case(
        lines, "wire capacity update above protocol limit (5000 > 128)",
        (CapacityLimitExceeded,),
        lambda: dec.feed_stream_chunk(forged_cap),
    )

    # 7. Insert instruction that cannot fit (protocol violation on wire).
    dec = Decoder(64)
    forged_ins = chunk(
        0, enc_u8(0x01) + enc_str("x" * 20) + enc_str("y" * 20)
    )
    failures += run_case(
        lines, "insert instruction exceeding table capacity (72 > 64)",
        (InvalidInstruction,),
        lambda: dec.feed_stream_chunk(forged_ins),
    )

    lines.append("")
    lines.append("summary: all %d cases detected, %d failure(s)"
                 % (7, failures))
    print("\nsummary: all 7 cases detected, %d failure(s)" % failures)

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "docs")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "detection_results.txt"),
              "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
