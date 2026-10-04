"""5 万段文档基准：报告展开耗时与内存峰值。

运行: python3 bench_50k.py [--paras 50000] [--runs 3]

指标口径：
- expand_seconds_min : 多次 expand() 的墙钟最小值（不含文档构造）
- iter_rows_seconds  : 100 万字符逐行流式遍历耗时
- rss_after_expand_bytes : 进程峰值 RSS（ru_maxrss，自进程启动起的高水位）
- tracemalloc_peak_bytes: expand() 期间纯 Python 对象分配峰值（单列）
"""
import argparse
import gc
import json
import os
import resource
import sys
import time
import tracemalloc
import zlib

from style_inherit import expand
from style_inherit.cases import bench_document

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def rss_bytes() -> int:
    # Linux 上 ru_maxrss 单位为 KB
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paras", type=int, default=50000)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    gc.collect()
    rss_start = rss_bytes()
    t0 = time.perf_counter()
    doc = bench_document(n_paras=args.paras)
    build_seconds = time.perf_counter() - t0
    rss_after_doc = rss_bytes()

    timings = []
    expanded = None
    for _ in range(args.runs):
        gc.collect()
        t0 = time.perf_counter()
        expanded = expand(doc)
        timings.append(time.perf_counter() - t0)
    rss_after_expand = rss_bytes()

    # tracemalloc 单独测量一次 expand 的 Python 对象峰值
    gc.collect()
    tracemalloc.start()
    expanded = expand(doc)
    _, tracemalloc_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 逐字符行流式遍历并计算校验和，确认 5 万段结果完整可用
    t0 = time.perf_counter()
    crc = 0
    n_chars = 0
    for row in expanded.iter_rows():
        crc = zlib.crc32(row.char.encode("utf-8"), crc)
        for value, source in zip(row.values, row.sources):
            crc = zlib.crc32(str(value).encode("utf-8"), crc)
            crc = zlib.crc32(source.encode("utf-8"), crc)
        n_chars += 1
    iter_rows_seconds = time.perf_counter() - t0

    n_runs = sum(len(p.runs) for p in doc.paragraphs)
    report = {
        "config": {
            "paragraphs": args.paras,
            "runs": n_runs,
            "chars": n_chars,
            "styles": len(doc.styles),
            "props": list(expanded.props),
        },
        "build_doc_seconds": round(build_seconds, 4),
        "expand_seconds_runs": [round(t, 4) for t in timings],
        "expand_seconds_min": round(min(timings), 4),
        "iter_rows_seconds": round(iter_rows_seconds, 4),
        "memory": {
            "rss_at_start_bytes": rss_start,
            "rss_after_doc_build_bytes": rss_after_doc,
            "rss_after_expand_bytes": rss_after_expand,
            "expand_rss_delta_bytes": rss_after_expand - rss_after_doc,
            "tracemalloc_peak_bytes": tracemalloc_peak,
        },
        "diagnostics_count": len(expanded.diagnostics),
        "checksum_crc32": "%08x" % crc,
        "python": sys.version.split()[0],
    }
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "bench_50k.json"), "w",
              encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
