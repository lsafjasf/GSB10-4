"""10 万节点基准：解析/写回耗时与内存峰值。

运行：python3 tests/benchmark.py
产物：results/benchmark.json、results/benchmark.md、results/bench_100k.json（生成的样本）
"""

import hashlib
import json
import os
import resource
import sys
import time
import tracemalloc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bookmarks.model import BookmarkDocument, FORMAT_ID, FORMAT_VERSION  # noqa: E402
from bookmarks import iterjson  # noqa: E402

RES = os.path.join(ROOT, "results")


def gen_100k():
    """确定性生成约 10 万节点：100 根目录 × 10 子目录 × 9 孙目录 × 10 链接。"""
    roots = []
    seq = 0

    def nid():
        nonlocal seq
        seq += 1
        return f"n{seq}"

    for r in range(100):
        root = {"id": nid(), "type": "folder", "name": f"根目录 {r}",
                "children": [], "add_date": "1759600000"}
        roots.append(root)
        for s in range(10):
            sub = {"id": nid(), "type": "folder", "name": f"子目录 {r}-{s}",
                   "children": [], "parent_id": root["id"]}
            root["children"].append(sub)
            for g in range(9):
                leaf_folder = {"id": nid(), "type": "folder",
                               "name": f"孙目录 {r}-{s}-{g}",
                               "children": [], "parent_id": sub["id"]}
                sub["children"].append(leaf_folder)
                for u in range(10):
                    leaf_folder["children"].append({
                        "id": nid(), "type": "url",
                        "name": f"书签 {r}-{s}-{g}-{u} 中文标题",
                        "url": f"https://example.com/{r}/{s}/{g}/{u}?q=测试",
                        "parent_id": leaf_folder["id"],
                        "tags": ["基准", f"组{u % 4}"],
                    })
    return {"format": FORMAT_ID, "version": FORMAT_VERSION,
            "meta": {"用途": "10 万节点基准"}, "roots": roots}


def gen_deep(depth=100_000):
    """10 万层单链目录（紧凑格式）。"""
    node = {"id": "leaf", "type": "url", "name": "底", "url": "https://deep/"}
    for level in range(depth, 0, -1):
        node = {"id": f"l{level}", "type": "folder", "name": f"L{level}",
                "children": [node]}
    return {"format": FORMAT_ID, "version": FORMAT_VERSION, "roots": [node]}


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def measure(doc_text, indent):
    """对一份文本测量解析与写回的耗时和内存峰值。"""
    result = {}
    result["input_bytes"] = len(doc_text.encode("utf-8"))

    tracemalloc.start()
    t0 = time.perf_counter()
    doc = BookmarkDocument.from_text(doc_text)
    t1 = time.perf_counter()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result["parse_seconds"] = round(t1 - t0, 3)
    result["parse_peak_mb"] = round(peak / 1024 / 1024, 1)
    result["issues"] = len(doc.report.issues)
    result["stats"] = doc.stats()

    tracemalloc.start()
    t0 = time.perf_counter()
    out = doc.to_text(indent=indent)
    t1 = time.perf_counter()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result["serialize_seconds"] = round(t1 - t0, 3)
    result["serialize_peak_mb"] = round(peak / 1024 / 1024, 1)
    result["output_bytes"] = len(out.encode("utf-8"))

    result["roundtrip_lossless"] = out == doc_text
    result["sha256_in"] = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
    result["sha256_out"] = hashlib.sha256(out.encode("utf-8")).hexdigest()
    result["process_peak_rss_mb"] = round(rss_mb(), 1)
    return result


def main():
    os.makedirs(RES, exist_ok=True)
    results = {"python": sys.version.split()[0],
               "platform": sys.platform, "cases": {}}

    print("生成 10 万节点样本 ...")
    t0 = time.perf_counter()
    tree = gen_100k()
    text = iterjson.dumps(tree)
    gen_s = time.perf_counter() - t0
    sample_path = os.path.join(RES, "bench_100k.json")
    with open(sample_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"  样本 {sample_path}（{len(text.encode('utf-8')) / 1e6:.1f} MB，"
          f"生成耗时 {gen_s:.2f}s）")

    print("测量 10 万节点（混合树，最大深度 4）...")
    results["cases"]["nodes_100k"] = measure(text, indent=2)
    results["cases"]["nodes_100k"]["generate_seconds"] = round(gen_s, 3)

    print("测量 10 万层超深单链 ...")
    deep_text = iterjson.dumps(gen_deep(), indent=None)
    results["cases"]["depth_100k"] = measure(deep_text, indent=None)

    with open(os.path.join(RES, "benchmark.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    lines = [
        "# 基准结果（Python " + results["python"] + "）",
        "",
        "| 用例 | 节点数 | 最大深度 | 文件大小 | 解析耗时 | 解析内存峰值 | 写回耗时 | 写回内存峰值 | 进程 RSS 峰值 | 往返 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, case in results["cases"].items():
        st = case["stats"]
        lines.append(
            f"| {name} | {st['nodes']} | {st['max_depth']} "
            f"| {case['input_bytes'] / 1e6:.1f} MB "
            f"| {case['parse_seconds']}s | {case['parse_peak_mb']} MB "
            f"| {case['serialize_seconds']}s | {case['serialize_peak_mb']} MB "
            f"| {case['process_peak_rss_mb']} MB "
            f"| {'无损 ✓' if case['roundtrip_lossless'] else '有差异 ✗'} |"
        )
    md = "\n".join(lines) + "\n"
    with open(os.path.join(RES, "benchmark.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    print()
    print(md)
    print(f"结果已写入 {os.path.join(RES, 'benchmark.json')} 与 benchmark.md")
    ok = all(c["roundtrip_lossless"] and c["issues"] == 0
             for c in results["cases"].values())
    print("基准校验：" + ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
