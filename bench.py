"""bench.py — 100 万位位图：编码选择表 + 空间/耗时对比 + 混合运算耗时。

运行：python3 bench.py   （生成 encoding_table.csv / bench_results.csv）
"""

import csv
import random
import time

from bitmap_codec import Bitmap, ENC_RAW, ENC_SPARSE, choose_encoding

N = 1_000_000
rng = random.Random(42)

DENSITIES = [0.0, 1e-6, 1e-5, 1e-4, 1e-3, 0.01, 0.05, 0.1, 0.2,
             0.5, 0.8, 0.9, 0.99, 0.999, 0.9999, 1.0]

ENC_NAME = {(ENC_RAW, False): "RAW", (ENC_SPARSE, False): "SPARSE",
            (ENC_SPARSE, True): "NSPARSE"}


def gen_positions(d):
    k = int(N * d)
    if k == 0:
        return []
    if k == N:
        return list(range(N))
    return sorted(rng.sample(range(N), k))


def fmt_bytes(n):
    for unit in ("B", "KiB", "MiB"):
        if n < 1024 or unit == "MiB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024


def main():
    table_rows, bench_rows = [], []
    bitmaps = {}
    print(f"{'density':>8} {'popcount':>8} {'chosen':>8} {'raw_bytes':>10} "
          f"{'sparse_bytes':>12} {'enc_ms':>8} {'dec_ms':>8}")
    for d in DENSITIES:
        pos = gen_positions(d)
        popcount = len(pos)
        enc, neg = choose_encoding(N, popcount)

        t0 = time.perf_counter()
        bm = Bitmap.from_positions(N, pos)
        enc_ms = (time.perf_counter() - t0) * 1e3

        t0 = time.perf_counter()
        v = bm.to_int()
        dec_ms = (time.perf_counter() - t0) * 1e3
        assert v.bit_count() == popcount

        raw_size = Bitmap.from_positions(N, pos, encoding=ENC_RAW).total_bytes()
        sparse_size = Bitmap.from_positions(
            N, pos, encoding=ENC_SPARSE).total_bytes()
        bitmaps[d] = bm

        chosen = ENC_NAME[(bm.encoding, bm.negated)]
        print(f"{d:>8} {popcount:>8} {chosen:>8} {raw_size:>10} "
              f"{sparse_size:>12} {enc_ms:>8.2f} {dec_ms:>8.2f}")
        table_rows.append([d, popcount, chosen, raw_size, sparse_size,
                           bm.total_bytes(), round(enc_ms, 3), round(dec_ms, 3)])

    # 混合运算耗时：极稀疏(SPARSE) x 半满(RAW) x 极稠密(NSPARSE)
    ops = [("AND", lambda a, b: a & b), ("OR", lambda a, b: a | b),
           ("XOR", lambda a, b: a ^ b), ("DIFF", lambda a, b: a - b)]
    pairs = [(0.0001, 0.5), (0.0001, 0.9999), (0.5, 0.9999), (0.5, 0.5)]
    print(f"\n{'pair(d1,d2)':>18} {'op':>5} {'encs':>16} {'op_ms':>8} "
          f"{'result_enc':>10} {'result_bytes':>12}")
    for d1, d2 in pairs:
        a, b = bitmaps[d1], bitmaps[d2]
        for op_name, op in ops:
            t0 = time.perf_counter()
            r = op(a, b)
            op_ms = (time.perf_counter() - t0) * 1e3
            encs = f"{ENC_NAME[(a.encoding,a.negated)]}x{ENC_NAME[(b.encoding,b.negated)]}"
            renc = ENC_NAME[(r.encoding, r.negated)]
            print(f"({d1},{d2})".rjust(18), op_name.rjust(5),
                  encs.rjust(16), f"{op_ms:>8.2f}", renc.rjust(10),
                  str(r.total_bytes()).rjust(12))
            bench_rows.append([f"({d1},{d2})", op_name, encs,
                               round(op_ms, 3), renc, r.total_bytes()])

    with open("encoding_table.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["density", "popcount", "chosen_encoding", "raw_bytes",
                    "sparse_bytes", "chosen_bytes", "encode_ms", "decode_ms"])
        w.writerows(table_rows)
    with open("bench_results.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pair", "op", "encodings", "op_ms",
                    "result_encoding", "result_bytes"])
        w.writerows(bench_rows)
    print("\n已写入 encoding_table.csv / bench_results.csv")


if __name__ == "__main__":
    main()
