"""Generate step-by-step encoder/decoder state comparison data.

Run:  python3 demo_trace.py
Output is written to docs/trace_step_by_step.txt and printed.
"""

import os
import sys

from dyntable import Decoder, Encoder


def dump_table(snap):
    entries = snap["entries"]
    if not entries:
        entries_text = "<empty>"
    else:
        entries_text = ", ".join(
            "#%d[%s=%s]" % (abs_idx, name, value)
            for abs_idx, name, value in entries
        )
    return (
        "cap=%(capacity)d size=%(size)d inserts=%(insert_count)d "
        "dropped=%(dropped_count)d entries=%(entries_text)s"
        % dict(snap, entries_text=entries_text)
    )


def main():
    lines = []

    def log(text):
        print(text)
        lines.append(text)

    enc = Encoder(max_capacity=140)
    dec = Decoder(max_capacity=140)
    step = 0
    mismatch = 0

    def record(op):
        nonlocal step, mismatch
        step += 1
        dec.feed_stream_chunk(enc.flush_stream_chunk())
        enc_snap = enc.table.snapshot()
        dec_snap = dec.table.snapshot()
        verdict = "MATCH" if enc_snap == dec_snap else "MISMATCH"
        if verdict != "MATCH":
            mismatch += 1
        log("STEP %02d | op: %s" % (step, op))
        log("  encoder: %s" % dump_table(enc_snap))
        log("  decoder: %s" % dump_table(dec_snap))
        log("  state : %s" % verdict)

    def record_block(headers, index=True):
        nonlocal step, mismatch
        step += 1
        block = enc.encode_header_block(headers, index=index)
        dec.feed_stream_chunk(enc.flush_stream_chunk())
        decoded = dec.decode_header_block(block)
        ok_headers = decoded == headers
        ok_table = enc.table.snapshot() == dec.table.snapshot()
        verdict = (
            "MATCH (headers + table)"
            if ok_headers and ok_table else "MISMATCH"
        )
        if not (ok_headers and ok_table):
            mismatch += 1
        log("STEP %02d | op: header block %s"
            % (step, headers))
        log("  wire  : %d bytes; decoded=%s" % (len(block), decoded))
        log("  state : %s" % verdict)

    log("== Dynamic table step-by-step cross-check ==")
    log("protocol maximum capacity: 140 bytes; "
        "entry overhead: 32 bytes")
    record("initial state (empty table)")

    enc.set_capacity(140)
    record("set_capacity(140)")

    for i in range(4):  # 4 x 34 = 136 <= 140 -> table full
        enc.insert("h", str(i))
        record("insert h=%d" % i)

    enc.insert("h", "4")  # 5th -> oldest h=0 evicted
    record("insert h=4 (table full -> evict oldest h=0)")

    record_block([("h", "4"), ("h", "1"), ("x-new", "1")])

    enc.set_capacity(70)  # shrink -> keep newest 2 entries
    record("set_capacity(70) (shrink triggers eviction)")

    for i in range(12):  # continuous insertion, constant churn
        enc.insert("k", str(i))
        record("insert k=%d (continuous insertion)" % i)

    record_block([("k", "11"), ("k", "5"), ("lit", "v")])

    enc.set_capacity(140)
    record("set_capacity(140) (grow)")
    record_block([(":status", "200"), ("k", "11"), ("x", "")])

    log("")
    log("summary: %d steps, %d mismatch(es)" % (step, mismatch))

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "docs")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "trace_step_by_step.txt"),
              "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if mismatch:
        sys.exit("state mismatch detected")


if __name__ == "__main__":
    main()
