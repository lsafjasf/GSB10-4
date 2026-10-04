#!/usr/bin/env python3
"""Self-test / demonstration for appendlog.

Run:  python3 selftest.py

Covers:
  1. empty file, single record
  2. crash points: partial header / partial payload / partial CRC field,
     recovery, then continue appending -> byte-for-byte 对拍 with an
     uninterrupted writer (sha256)
  3. middle corruption (two corrupt records) -> scan reports both with
     offsets; recovery truncates at the first corruption
  4. single-bit-flip detection: flip EVERY bit of a sample file
  5. false-positive statistics on clean files
  6. ~1 GiB file: recovery time and bytes read (bounded tail read only),
     plus full-scan time for contrast; 对拍 hash included
"""
import hashlib
import os
import random
import shutil
import sys
import time

import appendlog as al

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, "selftest_tmp")
FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f"  {detail}"
    print(line)
    if not cond:
        FAILURES.append(name)


def section(title):
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def payload(seed, n):
    """Deterministic payload known NOT to contain a magic (keeps crash-test
    parsing free of false magic candidates)."""
    p = random.Random(seed).randbytes(n)
    assert al.MAGIC not in p
    return p


def build_reference(path, payloads):
    with open(path, "wb") as f:
        for p in payloads:
            al.append_record(f, p)


def build_crashed(path, payloads, crash_rec, crash_bytes):
    with open(path, "wb") as f:
        for p in payloads[:crash_rec]:
            al.append_record(f, p)
        # process is kill -9'ed here: only a prefix of this record survives
        f.write(al.pack_record(payloads[crash_rec])[:crash_bytes])
        f.flush()
        os.fsync(f.fileno())


def record_offsets(payloads):
    offs, cur = [], 0
    for p in payloads:
        offs.append(cur)
        cur += al.OVERHEAD + len(p)
    return offs, cur


# ---------------------------------------------------------------------------
def test_edge_cases():
    section("1. Edge cases: empty file / single record")

    p_empty = os.path.join(TMP, "empty.log")
    open(p_empty, "wb").close()
    r = al.scan(p_empty)
    check("empty file: scan finds nothing",
          r.records == 0 and not r.errors and r.partial_tail is None,
          f"size={r.size}")
    rec = al.recover(p_empty)
    check("empty file: recover offset 0",
          rec.offset == 0 and rec.clean and rec.method == "empty",
          f"bytes_read={rec.bytes_read}")

    p_one = os.path.join(TMP, "single.log")
    pay = payload(42, 100)
    build_reference(p_one, [pay])
    r = al.scan(p_one)
    check("single record: scan 1/1",
          r.records == 1 and not r.errors and r.partial_tail is None and
          r.last_record_offset == 0 and r.last_record_end == os.path.getsize(p_one),
          f"last complete record [0..{r.last_record_end}), CRC-32 ok")
    rec = al.recover(p_one)
    check("single record: recover clean, no truncation",
          rec.clean and rec.offset == os.path.getsize(p_one),
          f"method={rec.method}, bytes_read={rec.bytes_read}")


def test_crash_points():
    section("2. Crash recovery: partial write in header / payload / CRC field")
    payloads = [payload(9000 + i, 64 + i * 3) for i in range(12)]
    ref = os.path.join(TMP, "ref.log")
    build_reference(ref, payloads)
    ref_hash = sha256(ref)
    print(f"  reference (uninterrupted) sha256 = {ref_hash}")

    rec7 = al.pack_record(payloads[7])
    expected_off = sum(al.OVERHEAD + len(p) for p in payloads[:7])
    crashes = [
        ("record header (6 of 8 header bytes)", 6),
        ("payload middle (header + half payload)", 8 + len(payloads[7]) // 2),
        ("CRC field (record minus last 2 bytes)", len(rec7) - 2),
    ]
    for name, cut in crashes:
        pth = os.path.join(TMP, f"crash_{cut}.log")
        build_crashed(pth, payloads, 7, cut)
        size_before = os.path.getsize(pth)
        res = al.recover(pth)
        with open(pth, "ab") as f:
            for p in payloads[7:]:
                al.append_record(f, p)
        h = sha256(pth)
        check(name,
              res.offset == expected_off and h == ref_hash and res.method == "backward",
              f"crashed_size={size_before}, trunc@{res.offset} "
              f"(expected {expected_off}), last complete "
              f"[{res.last_record_offset}..{res.last_record_end}) CRC-32 ok, "
              f"read={res.bytes_read} B, append-match={h == ref_hash}")


def test_middle_corruption():
    section("3. Middle corruption: two bad records must both be reported")
    payloads = [payload(7000 + i, 100) for i in range(10)]
    offs, total = record_offsets(payloads)
    ref = os.path.join(TMP, "mid_ref.log")
    build_reference(ref, payloads)
    ref_hash = sha256(ref)

    pth = os.path.join(TMP, "mid_bad.log")
    build_reference(pth, payloads)
    with open(pth, "r+b") as f:
        # flip a payload byte of record 3 and a CRC byte of record 7
        f.seek(offs[3] + 8 + 10)
        f.write(bytes([f.read(1)[0] ^ 0x01]))
        f.seek(offs[7] + 8 + len(payloads[7]) + 1)
        f.write(bytes([f.read(1)[0] ^ 0x01]))

    r = al.scan(pth)
    got = sorted(e.offset for e in r.errors)
    print("  reported corruptions:")
    for e in r.errors:
        print(f"    offset {e.offset:>4}  reason={e.reason}")
    check("both middle corruptions reported with exact offsets",
          got == [offs[3], offs[7]], f"expected {[offs[3], offs[7]]}")
    check("valid records after resync still counted", r.records == 8,
          f"records={r.records} (10 minus 2 corrupt)")
    check("good prefix ends right before first bad record",
          r.good_prefix_end == offs[3], f"good_prefix_end={r.good_prefix_end}")
    check("last complete valid record located",
          r.last_record_end == total and r.last_record_offset == offs[9],
          f"[{r.last_record_offset}..{r.last_record_end})")

    res = al.recover(pth)
    print(f"  recovery: method={res.method}, truncate@{res.offset}, "
          f"read={res.bytes_read} B, {len(res.errors)} corruptions found")
    check("recovery falls back to full scan and cuts at first corruption",
          res.method == "full-scan" and res.offset == offs[3],
          f"offset={res.offset} expected={offs[3]}")

    with open(pth, "ab") as f:
        for p in payloads[3:]:
            al.append_record(f, p)
    h = sha256(pth)
    check("after truncation + append, file matches uninterrupted reference",
          h == ref_hash, f"match={h == ref_hash}")


def test_bitflips():
    section("4. Single-bit-flip detection: flip every bit of a sample file")
    payloads = [payload(1000 + i, 40 + i * 7) for i in range(6)]
    pth = os.path.join(TMP, "bitflip.log")
    build_reference(pth, payloads)
    with open(pth, "rb") as f:
        original = f.read()
    size = len(original)
    total_bits = size * 8

    detected = 0
    t0 = time.perf_counter()
    for bit in range(total_bits):
        mutated = bytearray(original)
        mutated[bit >> 3] ^= 1 << (bit & 7)
        with open(pth, "wb") as f:
            f.write(mutated)
        r = al.scan(pth)
        if r.errors or r.partial_tail is not None or r.records != len(payloads):
            detected += 1
    elapsed = time.perf_counter() - t0
    build_reference(pth, payloads)  # restore
    check("every single-bit flip detected", detected == total_bits,
          f"{detected}/{total_bits} = {detected / total_bits:.4%} "
          f"in {elapsed:.2f}s (CRC-32 detects all bursts <= 32 bits; "
          f"undetected random corruption ~2^-32 = {2 ** -32:.2e})")


def test_false_positives():
    section("5. False-positive statistics on clean files")
    n_rec = 200_000
    payloads = [payload(5000 + i, 16) for i in range(n_rec)]
    pth = os.path.join(TMP, "clean.log")
    build_reference(pth, payloads)
    r = al.scan(pth)
    fp = len(r.errors)
    check("no false corruption reports on clean file",
          fp == 0 and r.records == n_rec and r.partial_tail is None,
          f"false positives = {fp}/{n_rec} records "
          f"({os.path.getsize(pth)} bytes verified)")
    print("  empirical false-positive rate: 0 "
          f"(upper bound < {1 / n_rec:.1e} per record)")


def test_large_file():
    section("6. ~1 GiB file: recovery time and bytes read (no full scan)")
    rec_size = 4 << 20                 # 4 MiB payload
    n_rec = 256                       # 256 * (4 MiB + 12) ~= 1.000 GiB
    big_payload = b"\x5a" * rec_size  # no MAGIC bytes inside
    free = shutil.disk_usage(TMP).free
    need = 2.3 * (n_rec + 1) * (rec_size + al.OVERHEAD)
    if free < need:
        n_rec = max(8, int(free / 2.3 / (rec_size + al.OVERHEAD)) - 1)
        print(f"  (disk space adjusted test to {n_rec} records)")

    ref = os.path.join(TMP, "big_ref.log")
    pth = os.path.join(TMP, "big_crashed.log")

    t0 = time.perf_counter()
    build_reference(ref, [big_payload] * (n_rec + 1))
    t_build = time.perf_counter() - t0
    ref_size = os.path.getsize(ref)
    ref_hash = sha256(ref)

    # crash: N complete records + half of the last record (header+half body)
    with open(pth, "wb") as f:
        for _ in range(n_rec):
            al.append_record(f, big_payload)
        partial = al.pack_record(big_payload)
        f.write(partial[:8 + rec_size // 2])
        f.flush()
        os.fsync(f.fileno())
    crashed_size = os.path.getsize(pth)
    expected_off = n_rec * (rec_size + al.OVERHEAD)

    t0 = time.perf_counter()
    res = al.recover(pth)
    t_rec = time.perf_counter() - t0

    with open(pth, "ab") as f:
        al.append_record(f, big_payload)
    h = sha256(pth)

    print(f"  file size (crashed)   : {crashed_size:,} B "
          f"({crashed_size / 2 ** 30:.3f} GiB)")
    print(f"  recovery method       : {res.method}")
    print(f"  truncation offset     : {res.offset:,} (expected "
          f"{expected_off:,}, match={res.offset == expected_off})")
    print(f"  last complete record  : [{res.last_record_offset:,}.."
          f"{res.last_record_end:,})  basis: magic + length + CRC-32 verified")
    print(f"  recovery time         : {t_rec * 1000:.2f} ms")
    print(f"  bytes read to recover : {res.bytes_read:,} B "
          f"({res.bytes_read / 2 ** 20:.2f} MiB = "
          f"{res.bytes_read / crashed_size:.4%} of file)")
    print(f"  append after recovery : sha256 match uninterrupted = {h == ref_hash}")
    print(f"  (reference build time : {t_build:.2f}s, full size "
          f"{ref_size / 2 ** 30:.3f} GiB)")

    check("1 GiB recovery uses bounded tail read only",
          res.method == "backward" and res.offset == expected_off and
          res.bytes_read <= 8 << 20 and res.bytes_read < crashed_size // 100,
          f"read {res.bytes_read / 2 ** 20:.2f} MiB of "
          f"{crashed_size / 2 ** 30:.3f} GiB in {t_rec * 1000:.2f} ms")
    check("1 GiB append after recovery matches uninterrupted file",
          h == ref_hash)

    # contrast: full verification must read everything (only for comparison)
    t0 = time.perf_counter()
    r = al.scan(ref)
    t_scan = time.perf_counter() - t0
    print(f"  contrast: full scan() of {ref_size / 2 ** 30:.3f} GiB takes "
          f"{t_scan:.2f}s and reads {ref_size / 2 ** 20:.0f} MiB "
          f"(records={r.records}, errors={len(r.errors)})")


def main():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    print("appendlog self-test")
    print(f"format: 4B magic {al.MAGIC!r} | 4B LE length | payload | "
          f"4B CRC-32(magic+length+payload)")
    try:
        test_edge_cases()
        test_crash_points()
        test_middle_corruption()
        test_bitflips()
        test_false_positives()
        test_large_file()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "=" * 78)
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL CHECKS PASSED")
    print("=" * 78)


if __name__ == "__main__":
    main()
