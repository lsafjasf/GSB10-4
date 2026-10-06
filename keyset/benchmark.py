"""benchmark.py — 1000 万行深度翻页基准。

做三件事：
  A. 建表灌 1000 万行（score 随机、有重复），建 (score, id) 复合索引；
  B. 键集分页从第 1 页翻到最后一页，记录单页耗时曲线、总耗时、单页上界；
  C. 对照组 OFFSET 在不同页深各取一页，展示耗时随页号线性增长并外推末页。

运行： python3 benchmark.py            # 首次自动建库，之后复用 /tmp 下的库
       python3 benchmark.py --fresh    # 强制重建
"""

import argparse
import json
import os
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keyset_page import KeysetPaginator, offset_page

N = 10_000_000
PAGE = 1_000
DB = "/tmp/keyset_bench_10m.db"
SAMPLE_PAGES = [1, 10, 100, 500, 1_000, 2_000, 4_000, 6_000, 8_000, 9_999, 10_000]
OFFSET_PAGES = [1, 500, 1_000, 2_000, 4_000, 8_000]
TOTAL_PAGES = N // PAGE


def build_db(fresh):
    if fresh and os.path.exists(DB):
        os.remove(DB)
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA synchronous=OFF")
    has_t = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='t'"
    ).fetchone()[0]
    if has_t:
        cnt = conn.execute("SELECT count(*) FROM t").fetchone()[0]
        if cnt == N and conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='index' AND name='idx_t_score'"
        ).fetchone()[0]:
            print(f"复用已建库 {DB}（{N} 行，索引已存在）")
            return conn
        conn.execute("DROP TABLE t")
    print(f"建库：{N} 行 -> {DB}")
    t0 = time.perf_counter()
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, score INTEGER NOT NULL)")
    rng = random.Random(42)
    ins = "INSERT INTO t(id, score) VALUES (?, ?)"
    batch, total = [], 0
    for i in range(1, N + 1):
        batch.append((i, rng.randrange(0, 1_000_000_000)))
        if len(batch) == 100_000:
            conn.executemany(ins, batch)
            total += len(batch)
            batch.clear()
            if total % 1_000_000 == 0:
                print(f"  灌入 {total // 1_000_000}/10 M, 用时 {time.perf_counter()-t0:.1f}s")
    if batch:
        conn.executemany(ins, batch)
    print("  建复合索引 (score, id) ...")
    conn.execute("CREATE INDEX idx_t_score ON t(score, id)")
    conn.commit()
    print(f"建库完成，总用时 {time.perf_counter()-t0:.1f}s")
    return conn


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def bench_keyset(conn):
    print(f"\n[A] 键集分页全量翻页（page_size={PAGE}，共 {TOTAL_PAGES} 页）")
    pag = KeysetPaginator(conn, "t", "score", "id", PAGE)
    samples, all_ms = {}, []
    total_rows = 0

    t0 = time.perf_counter()
    page = pag.first_page()
    dt = (time.perf_counter() - t0) * 1000
    all_ms.append(dt)
    if 1 in SAMPLE_PAGES:
        samples[1] = dt
    total_rows += len(page.rows)

    while page.has_next:
        cursor, next_no = page.cursor, page.page_no + 1
        t0 = time.perf_counter()
        page = pag.next_page(cursor, next_no)
        dt = (time.perf_counter() - t0) * 1000
        all_ms.append(dt)
        if page.page_no in SAMPLE_PAGES:
            samples[page.page_no] = dt
        total_rows += len(page.rows)

    total = sum(all_ms) / 1000.0
    assert total_rows == N, total_rows
    assert len(all_ms) == TOTAL_PAGES, len(all_ms)
    return samples, all_ms, total


def _time_offset(conn, page_no):
    t0 = time.perf_counter()
    rows = offset_page(conn, "t", "score", "id", PAGE, page_no)
    dt = (time.perf_counter() - t0) * 1000
    assert len(rows) == PAGE
    return dt


def bench_offset(conn):
    print(f"[B] 对照组 OFFSET 单页取页（page_size={PAGE}，每点取 2 次最小值）")
    out = {}
    for pn in OFFSET_PAGES:
        best = min(_time_offset(conn, pn) for _ in range(2))
        out[pn] = best
        print(f"  第 {pn:>5} 页 (OFFSET {pn * PAGE:>8}): {best:8.2f} ms")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()
    conn = build_db(args.fresh)

    samples, all_ms, total = bench_keyset(conn)
    off = bench_offset(conn)

    # OFFSET 耗时线性拟合 ms = a + b * offset_rows
    xs = [pn * PAGE for pn in OFFSET_PAGES]
    ys = [off[pn] for pn in OFFSET_PAGES]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    a = my - b * mx
    last_ms = a + b * (N - PAGE)
    full_offset_s = sum(a + b * pn * PAGE for pn in range(1, TOTAL_PAGES + 1)) / 1000.0

    L = []
    L.append("# 1000 万行深度翻页基准结果\n")
    L.append(f"- 数据量: {N:,} 行（score 为 0..1e9 随机整数，有重复）")
    L.append(f"- page_size={PAGE:,}，总页数 {TOTAL_PAGES:,}；索引: (score, id) 复合 B+树")
    L.append("- 计时含 SQL 执行 + fetchall 取页；OFFSET 每点取 2 次最小值\n")

    L.append("## 键集分页：页号 vs 单页耗时")
    L.append("| 页号 | 若用 OFFSET 需跳过 | 键集单页耗时 (ms) |")
    L.append("|---:|---:|---:|")
    for pn in SAMPLE_PAGES:
        L.append(f"| {pn:,} | {(pn - 1) * PAGE:,} | {samples[pn]:.3f} |")
    L.append(f"\n- **翻到最后一页（全量 10000 页 / 1000 万行）总耗时: {total:.2f} s**")
    L.append(f"- **单页耗时上界 max: {max(all_ms):.3f} ms**")
    L.append(f"- 单页均值 {sum(all_ms)/len(all_ms):.3f} ms，"
             f"p50 {pct(all_ms,.5):.3f} ms，p95 {pct(all_ms,.95):.3f} ms\n")

    L.append("## 对照组 OFFSET：单页耗时随页号线性增长")
    L.append("| 页号 | OFFSET 行数 | 单页耗时 (ms) |")
    L.append("|---:|---:|---:|")
    for pn in OFFSET_PAGES:
        L.append(f"| {pn:,} | {pn * PAGE:,} | {off[pn]:.2f} |")
    L.append(f"\n- 线性拟合: 耗时 ≈ {a:.2f} + {b * 1e9:.4f} ms / 百万行扫描")
    L.append(f"- 外推末页(第 {TOTAL_PAGES:,} 页)单页 ≈ **{last_ms/1000:.1f} s**，"
             f"而键集末页实测 {samples[TOTAL_PAGES]:.3f} ms")
    L.append(f"- OFFSET 全量翻 10000 页约需 {full_offset_s:,.0f} s（{full_offset_s/3600:.1f} 小时），"
             f"键集 {total:.1f} s")

    report = "\n".join(L)
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results_10m.md")
    with open(out_path, "w") as f:
        f.write(report + "\n")
    print("\n" + report)
    print(f"\n已写入 {out_path}")


if __name__ == "__main__":
    main()
