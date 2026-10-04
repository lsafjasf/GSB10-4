# -*- coding: utf-8 -*-
"""10 万节点基准：解析与写回耗时、内存峰值。

两种形态：
* balanced_100000：约 10 万节点的宽树（100 个一级目录，每个 100 个子目录各放 ~9 个书签）
* chain_100000：10 万层目录串成一条链（极端超深嵌套，紧凑输出）

运行：python3 benchmark.py
产物：out/benchmark_report.json、out/benchmark_*.html
"""

import json
import os
import resource
import time
import tracemalloc

import bookmarks

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
TARGET = 100000


def _rss_kb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # Linux: KiB


def add_bookmark(folder, idx):
    node = bookmarks.Node("bookmark", title="链接 %d" % idx)
    node.attrs["HREF"] = "https://bench.example/path/%d" % idx
    node.attrs["ADD_DATE"] = str(1700000000 + idx)
    node.attrs["ID"] = "b%d" % idx
    node.attrs["PID"] = folder.get_attr("id")
    node.parent = folder
    folder.children.append(node)


def build_balanced_tree(target=TARGET):
    tree = bookmarks.BookmarkTree()
    count = 0
    f = 0
    while count < target:
        top = bookmarks.Node("folder", title="一级目录 %d" % f)
        top.attrs["ID"] = "top%d" % f
        top.parent = tree.root
        tree.root.children.append(top)
        count += 1
        for sub_index in range(100):
            if count >= target:
                break
            sub = bookmarks.Node("folder", title="子目录 %d-%d" % (f, sub_index))
            sub.attrs["ID"] = "sub-%d-%d" % (f, sub_index)
            sub.parent = top
            top.children.append(sub)
            count += 1
            for leaf in range(9):
                if count >= target:
                    break
                add_bookmark(sub, count)
                count += 1
        f += 1
    return tree


def build_chain_tree(target=TARGET):
    tree = bookmarks.BookmarkTree()
    node = tree.root
    for level in range(target - 1):
        folder = bookmarks.Node("folder", title="层 %d" % level)
        folder.attrs["ID"] = "f%d" % level
        folder.parent = node
        node.children.append(folder)
        node = folder
    leaf = bookmarks.Node("bookmark", title="链底书签")
    leaf.attrs["HREF"] = "https://chain.example/"
    leaf.parent = node
    node.children.append(leaf)
    return tree


def run_case(name, tree, indent, path):
    total, depth = bookmarks.stats(tree)
    result = {"case": name, "nodes": total, "max_depth": depth, "indent": indent}

    # ---- 写回（树 -> 文件）----
    tracemalloc.start()
    t0 = time.perf_counter()
    text = bookmarks.serialize(tree, indent=indent)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    write_sec = time.perf_counter() - t0
    write_current, write_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    result["file_bytes"] = os.path.getsize(path)
    result["write_seconds"] = round(write_sec, 4)
    result["write_peak_python_bytes"] = write_peak  # tracemalloc 峰值（Python 分配）

    # ---- 解析（文件 -> 树）----
    del tree
    tracemalloc.start()
    t0 = time.perf_counter()
    with open(path, "r", encoding="utf-8") as handle:
        parsed = bookmarks.parse_string(handle.read())
    parse_sec = time.perf_counter() - t0
    parse_current, parse_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    result["parse_seconds"] = round(parse_sec, 4)
    result["parse_peak_python_bytes"] = parse_peak
    result["parse_preserves_count"] = bookmarks.stats(parsed)[0] == total

    # ---- 二次写回对拍（不额外计入耗时，仅确认无损）----
    reparsed = bookmarks.parse_string(bookmarks.serialize(parsed, indent=indent))
    result["roundtrip_lossless"] = bookmarks.trees_equal(parsed, reparsed)
    result["nodes_per_second_parse"] = int(total / parse_sec) if parse_sec else None
    result["nodes_per_second_write"] = int(total / write_sec) if write_sec else None
    return result, parsed


def main():
    os.makedirs(OUT, exist_ok=True)
    rss0 = _rss_kb()
    results = []

    tree = build_balanced_tree()
    result, _ = run_case(
        "balanced_100000", tree, indent=4,
        path=os.path.join(OUT, "benchmark_balanced.html"))
    del tree
    results.append(result)

    tree = build_chain_tree()
    result, _ = run_case(
        "chain_100000", tree, indent=0,
        path=os.path.join(OUT, "benchmark_chain.html"))
    del tree
    results.append(result)

    rss_end = _rss_kb()
    report = {
        "环境": {
            "python": os.popen("python3 --version").read().strip(),
            "测量方式": "time.perf_counter 计时；tracemalloc 统计 Python 堆峰值；"
                      "ru_maxrss 为进程常驻内存峰值（KiB，跨全部用例累计）",
        },
        "process_rss_peak_kib_before": rss0,
        "process_rss_peak_kib_after": rss_end,
        "process_rss_peak_delta_kib": rss_end - rss0,
        "用例": results,
    }
    with open(os.path.join(OUT, "benchmark_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print("%-18s %8s %10s %10s %12s %12s"
          % ("case", "nodes", "parse_s", "write_s", "parse_peak", "write_peak"))
    for item in results:
        print("%-18s %8d %10.3f %10.3f %10.1fMiB %10.1fMiB 无损=%s"
              % (item["case"], item["nodes"], item["parse_seconds"],
                 item["write_seconds"],
                 item["parse_peak_python_bytes"] / 1048576,
                 item["write_peak_python_bytes"] / 1048576,
                 item["roundtrip_lossless"]))
    print("进程 RSS 峰值: %d KiB (%.1f MiB)" % (rss_end, rss_end / 1024))


if __name__ == "__main__":
    main()
