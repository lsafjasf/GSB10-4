"""10 万单元格基准: 解析耗时 + 峰值内存, 并验证内存不随行列最大值平方增长。

两种布局, 同样 10 万个单元格、同样的 SST:
  compact: 坐标集中在 10000 行 x 10 列;
  spread : 同样 10 万格, 但行最大 200 万、列最大 45001 (若按稠密二维存储
           需要约 9e10 个槽位, 直接爆内存; 本实现稀疏存储, 内存应基本相同)。
"""
import os
import random
import time
import tracemalloc
import resource

from shared_strings import (
    RichText, Run, SharedStringTable, Sheet,
    build_sst, dump_sst, dump_sheet, format_coord,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "bench")
N = 100_000


def make_cells(rng, n):
    formats = [("b", []), ("i", []), ("color", [("rgb", "FF0066CC")]), ("sz", [("val", "12")])]
    pool = []
    for i in range(2000):
        if i % 5 == 0:
            pool.append(RichText((
                Run(f"加粗{i}", (("b", ()),)),
                Run(f"普通{i}", ()),
            )))
        else:
            pool.append(RichText((Run(f"共享文本{i:04d}-" + "值" * (i % 17)),)))
    cells = {}
    while len(cells) < n:
        cells[(rng.randint(1, 1 << 30), rng.randint(1, 100))] = rng.choice(pool)
    return cells, pool


def main():
    os.makedirs(OUT, exist_ok=True)
    rng = random.Random(42)
    raw, pool = make_cells(rng, N)

    layouts = {
        "compact": sorted(((i // 10) + 1, (i % 10) + 1, value) for i, ((_r, _c), value) in enumerate(sorted(raw.items()))),
        "spread": sorted((i * 20 + 1, (i % 10) * 5000 + 1, value) for i, ((_r, _c), value) in enumerate(sorted(raw.items()))),
    }
    entries, index = build_sst(pool)
    sst_xml = dump_sst(entries)
    with open(os.path.join(OUT, "sharedStrings.xml"), "w", encoding="utf-8") as fh:
        fh.write(sst_xml)

    lines = [
        "10 万单元格解析基准 (Python %d.%d, 仅标准库)" % __import__("sys").version_info[:2],
        "=" * 64,
        f"单元格数: {N}, 共享表条目数: {len(entries)}, 含富文本条目: {sum(1 for e in entries if len(e.runs) > 1)}",
        "",
        f"{'布局':<8} {'最大行':>9} {'最大列':>8} {'文件KB':>9} {'解析ms':>9} {'峰值MB':>9}",
    ]
    results = {}
    for label, layout in layouts.items():
        cells = {(r, c): v for r, c, v in layout}
        xml = dump_sheet(cells, sst_index=index)
        path = os.path.join(OUT, f"sheet_{label}.xml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(xml)

        sst = SharedStringTable.from_xml(sst_xml)
        tracemalloc.start()
        t0 = time.perf_counter()
        sheet = Sheet.from_xml(path, sst, f"bench_{label}")
        elapsed = time.perf_counter() - t0
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert len(sheet) == N, len(sheet)
        assert sheet.get(layout[0][0], layout[0][1]).text == layout[0][2].text
        max_r = max(r for r, _c in cells)
        max_c = max(c for _r, c in cells)
        peak_mb = peak / (1024 * 1024)
        results[label] = peak_mb
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        lines.append(
            f"{label:<8} {max_r:>9} {max_c:>8} {len(xml.encode()) / 1024:>9.1f} "
            f"{elapsed * 1000:>9.1f} {peak_mb:>9.2f}"
        )

    ratio = max(results.values()) / min(results.values())
    lines += [
        "",
        "稀疏性校验:",
        f"  spread/compact 峰值内存比 = {ratio:.2f} (接近 1 即内存不随行列最大值平方增长)",
        f"  spread 若用稠密二维数组需 ~{2_000_000 * 45_001 / 1e8:.0f} 亿槽位, 实测仅线性存储 {N} 格",
        f"  进程 ru_maxrss 峰值 (含解释器, Linux KB): {rss_kb} KB = {rss_kb/1024:.1f} MB",
        "  tracemalloc 峰值仅含解析阶段 Python 对象分配",
    ]
    report = "\n".join(lines) + "\n"
    with open(os.path.join(HERE, "data", "benchmark.txt"), "w", encoding="utf-8") as fh:
        fh.write(report)
    print(report)


if __name__ == "__main__":
    main()
