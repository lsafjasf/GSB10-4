"""appendlog -- append-only record log with CRC-32 integrity, scanning and crash recovery.

Record layout (little-endian)::

    magic    4 bytes   b"ALOG"
    length   4 bytes   payload size in bytes (uint32)
    payload  length bytes
    crc32    4 bytes   CRC-32 of (magic + length + payload)

Integrity guarantees
--------------------
* Any single-bit flip inside a record (header, payload or the stored CRC
  field) is always detected: CRC-32 detects every burst error of <= 32 bits
  and a single-bit flip is a burst of length 1.  A flip in the magic is
  caught by the framing check.
* For arbitrary random (multi-byte) corruption the probability of an
  undetected error is ~2**-32 (~2.3e-10).

Recovery strategy
-----------------
``recover()`` does NOT scan the whole file in the common case.  It reads a
bounded tail window (default 8 MiB, grown only if no record boundary is
found), locates candidate record starts by their magic, and parses forward
from the earliest candidate that verifies cleanly through CRC checks up to
EOF.  The truncation point is the start of the incomplete tail record (or
EOF when the tail is intact).  If any inconsistency is found inside the
window, it falls back to a full streaming ``scan()`` and truncates at the
end of the last valid record before the first corruption.
"""
from __future__ import annotations

import mmap
import os
import struct
import zlib
from dataclasses import dataclass, field

MAGIC = b"ALOG"
HEADER = struct.Struct("<4sI")          # magic + payload length
TRAILER = struct.Struct("<I")           # crc32
OVERHEAD = HEADER.size + TRAILER.size
MAX_PAYLOAD = 1 << 30                   # sanity bound for the length field


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def pack_record(payload: bytes) -> bytes:
    header = HEADER.pack(MAGIC, len(payload))
    crc = zlib.crc32(header)
    crc = zlib.crc32(payload, crc) & 0xFFFFFFFF
    return header + payload + TRAILER.pack(crc)


def append_record(fileobj, payload: bytes) -> int:
    """Append one record to an open binary file; returns bytes written."""
    data = pack_record(payload)
    fileobj.write(data)
    return len(data)


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------

@dataclass
class Corruption:
    offset: int
    reason: str


@dataclass
class ScanResult:
    path: str
    size: int
    records: int = 0
    errors: list = field(default_factory=list)
    last_record_offset: int | None = None   # start of last complete valid record
    last_record_end: int = 0                # end (exclusive) of last valid record
    good_prefix_end: int = 0                # end of last valid record before 1st error
    partial_tail: int | None = None         # offset where an incomplete tail begins


def _scan_buffer(buf, base: int = 0, pos: int = 0) -> list:
    """Parse records in ``buf`` (covering absolute offsets [base, base+len(buf)))
    starting at buffer index ``pos``.  Returns a list of events:
    ("record", abs_off, abs_end, payload_len) | ("error", abs_off, reason)
    | ("partial", abs_off).  Stops after a "partial" event (tail of file)."""
    events = []
    n = len(buf)
    while pos < n:
        if n - pos < HEADER.size:
            events.append(("partial", base + pos))
            break
        magic, length = HEADER.unpack_from(buf, pos)
        if magic != MAGIC:
            events.append(("error", base + pos, "bad magic"))
            nxt = buf.find(MAGIC, pos + 1)
            if nxt < 0:
                break
            pos = nxt
            continue
        if length > MAX_PAYLOAD:
            events.append(("error", base + pos, "implausible length"))
            nxt = buf.find(MAGIC, pos + 1)
            if nxt < 0:
                break
            pos = nxt
            continue
        rec_end = pos + OVERHEAD + length
        if rec_end > n:
            events.append(("partial", base + pos))
            break
        stored = TRAILER.unpack_from(buf, rec_end - TRAILER.size)[0]
        crc = zlib.crc32(buf[pos:rec_end - TRAILER.size]) & 0xFFFFFFFF
        if crc != stored:
            events.append(("error", base + pos, "crc mismatch"))
            nxt = buf.find(MAGIC, pos + 1)
            if nxt < 0:
                break
            pos = nxt
            continue
        events.append(("record", base + pos, base + rec_end, length))
        pos = rec_end
    return events


def scan(path: str) -> ScanResult:
    """Full streaming verification of the whole file.

    Reports every corrupt region (not just the tail), resynchronising on the
    next magic after each error, plus the offset/CRC status of the last
    complete record."""
    size = os.path.getsize(path)
    res = ScanResult(path=path, size=size)
    if size == 0:
        return res
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            events = _scan_buffer(mm)
        finally:
            mm.close()
    seen_error = False
    for ev in events:
        kind = ev[0]
        if kind == "record":
            res.records += 1
            res.last_record_offset = ev[1]
            res.last_record_end = ev[2]
            if not seen_error:
                res.good_prefix_end = ev[2]
        elif kind == "error":
            res.errors.append(Corruption(ev[1], ev[2]))
            seen_error = True
        else:  # partial
            res.partial_tail = ev[1]
    return res


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------

@dataclass
class RecoverResult:
    path: str
    offset: int                 # file size after recovery (truncation point)
    clean: bool                 # True when no truncation was needed
    bytes_read: int             # bytes actually read to decide
    method: str                 # "empty" | "backward" | "full-scan"
    errors: list = field(default_factory=list)
    last_record_offset: int | None = None
    last_record_end: int = 0


def recover(path: str, window: int = 8 << 20, apply: bool = True) -> RecoverResult:
    """Truncate ``path`` at the end of the last complete, CRC-valid record so
    that appending can safely resume.

    Fast path ("backward"): inspects only a bounded tail window.  Falls back
    to a full scan when the tail window shows any inconsistency (e.g. middle
    corruption) or no record boundary can be found."""
    size = os.path.getsize(path)
    if size == 0:
        return RecoverResult(path, 0, True, 0, "empty")

    bytes_read = 0
    w = min(window, size)
    with open(path, "rb") as f:
        while True:
            start = size - w
            f.seek(start)
            buf = f.read(w)
            bytes_read += len(buf)

            # candidate record starts (magic positions), ascending
            cands = []
            i = buf.find(MAGIC)
            while i >= 0:
                cands.append(i)
                i = buf.find(MAGIC, i + 1)

            decided = None
            decided_last = None
            saw_error = False
            for c in cands:
                events = _scan_buffer(buf, base=start, pos=c)
                ok = True
                trunc = None
                last = None
                for ev in events:
                    if ev[0] == "error":
                        ok = False
                        break
                    if ev[0] == "partial":
                        trunc = ev[1]
                    else:
                        last = (ev[1], ev[2])
                if ok:
                    decided = trunc if trunc is not None else size
                    decided_last = last
                    break
                saw_error = True

            if decided is not None and not saw_error:
                clean = decided == size
                if apply and not clean:
                    os.truncate(path, decided)
                return RecoverResult(
                    path, decided, clean, bytes_read, "backward", [],
                    decided_last[0] if decided_last else None,
                    decided_last[1] if decided_last else decided,
                )
            if saw_error:
                break  # inconsistency inside window -> full scan
            if start == 0:
                break  # whole file inspected, no usable boundary
            w = min(w * 2, size)  # grow window towards the front of the file

    # conservative fallback: verify the whole file, cut at first corruption
    res = scan(path)
    bytes_read += size
    if res.errors:
        off = res.good_prefix_end
    elif res.partial_tail is not None:
        off = res.partial_tail
    else:
        off = size
    if apply and off != size:
        os.truncate(path, off)
    return RecoverResult(path, off, off == size, bytes_read, "full-scan",
                         res.errors, res.last_record_offset, res.last_record_end)
