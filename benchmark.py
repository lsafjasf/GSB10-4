#!/usr/bin/env python3
"""基准：5000 个图标项的校验耗时与目录遍历次数。

分布（确定构造，无随机）：
  60% 精确尺寸命中   15% 仅其他尺寸(nearest)   10% 仅 scalable
  10% 完全缺失        5% 主题内是坏文件、由 hicolor 兜底命中
对照：索引模式（建索引一次 + 纯 dict 查询）vs naive 逐路径 stat 探测。
"""
import os
import sys
import tempfile
import time

from icon_theme import (CONTEXTS, FIXED_SIZES, IconTheme, TraversalCounter,
                        naive_probe)

N = 5000
PNG_OK = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
          + b"\x00" * 13 + b"\x00" * 4)
SVG_OK = '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"/>\n'


def write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(data)


def build(root):
    write(root, "bench/index.theme", "[Icon Theme]\nInherits=base\n")
    write(root, "base/index.theme", "[Icon Theme]\n")
    write(root, "hicolor/index.theme", "[Icon Theme]\n")
    counts = {"exact": 0, "nearest": 0, "scalable": 0,
              "missing": 0, "corrupt_fallback": 0}
    for i in range(N):
        name = f"icon-{i:05d}"
        ctx = CONTEXTS[i % len(CONTEXTS)]
        size = FIXED_SIZES[i % len(FIXED_SIZES)]
        cat = i % 100
        if cat < 60:
            write(root, f"bench/{size}/{ctx}/{name}.png", PNG_OK)
            counts["exact"] += 1
        elif cat < 75:
            other = 16 if size != 16 else 256
            write(root, f"bench/{other}/{ctx}/{name}.png", PNG_OK)
            counts["nearest"] += 1
        elif cat < 85:
            write(root, f"bench/scalable/{ctx}/{name}.svg", SVG_OK)
            counts["scalable"] += 1
        elif cat < 95:
            counts["missing"] += 1
        else:
            write(root, f"bench/{size}/{ctx}/{name}.png", PNG_OK[:20])  # 截断
            write(root, f"hicolor/{size}/{ctx}/{name}.png", PNG_OK)
            counts["corrupt_fallback"] += 1
    return counts


def main():
    root = tempfile.mkdtemp(prefix="icontheme-bench-")
    dist = build(root)
    items = [(f"icon-{i:05d}", CONTEXTS[i % len(CONTEXTS)],
              FIXED_SIZES[i % len(FIXED_SIZES)]) for i in range(N)]

    # 1) 建索引（含目录约定校验）
    counter = TraversalCounter()
    t0 = time.perf_counter()
    theme = IconTheme(root, counter)
    t1 = time.perf_counter()
    index_scandir = counter.scandir

    # 2) 索引模式解析 5000 项（含完整性检查，按路径缓存）
    t2 = time.perf_counter()
    results = [theme.resolve("bench", n, c, s) for n, c, s in items]
    t3 = time.perf_counter()
    status = {}
    bad_total = 0
    for r in results:
        status[r.status] = status.get(r.status, 0) + 1
        bad_total += len(r.bad_files)

    # 3) naive 逐路径探测（无索引、无完整性检查，作遍历次数对照）
    naive_counter = TraversalCounter()
    chain = theme.theme_chain("bench")
    t4 = time.perf_counter()
    naive_paths = [naive_probe(root, chain, n, c, s, naive_counter)
                   for n, c, s in items]
    t5 = time.perf_counter()
    naive_hits = sum(p is not None for p in naive_paths)
    # 对拍：naive 与索引模式命中路径不一致的条数（坏文件被错当命中）
    disagreements = sum(
        p != (r.hit.path if r.hit else None)
        for p, r in zip(naive_paths, results))

    print(f"图标项: {N}  分布: {dist}")
    print(f"主题链: {' -> '.join(chain)}")
    print()
    print(f"[索引] 建索引+约定校验 : {(t1 - t0) * 1e3:8.2f} ms  "
          f"scandir={index_scandir}")
    print(f"[索引] 解析 {N} 项     : {(t3 - t2) * 1e3:8.2f} ms  "
          f"scandir=0  file_probe(完整性)={counter.file_probe}")
    print(f"[索引] 状态分布        : {status}  链上坏文件共 {bad_total} 个")
    print(f"[naive] 探测 {N} 项    : {(t5 - t4) * 1e3:8.2f} ms  "
          f"stat 探测={naive_counter.file_probe}  命中={naive_hits}")
    print(f"对拍差异（naive 把坏文件当命中）: {disagreements} 条")
    print()
    print("说明: naive 不做坏文件检查，虽然命中数与索引模式相同，但其中 250 条")
    print("      指向 bench 主题里的截断 PNG；索引模式判其为坏文件并继续回退，")
    print("      最终命中 hicolor 的有效文件，且把坏文件单独列出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
