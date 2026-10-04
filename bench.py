"""5 万段文档展开基准：耗时 + 内存峰值（tracemalloc 与 ru_maxrss 双口径）。"""

import os
import random
import resource
import sys
import time
import tracemalloc

from style_expand import StyleSheet, expand_document

N_PARAS = 50_000
N_STYLES = 500
CHAIN = 12


def build():
    rng = random.Random(7)
    styles = {"Root": {"props": {"font": "serif", "size": 12, "color": "black",
                                 "bold": False, "italic": False}, "based_on": None}}
    names = ["Root"]
    for i in range(N_STYLES):
        name = f"St{i}"
        styles[name] = {"props": {"size": rng.choice([10, 12, 14, 16])},
                        "based_on": names[-1] if len(names) < CHAIN else names[-CHAIN]}
        names.append(name)

    doc = []
    for i in range(N_PARAS):
        text = f"para{i:05d}-the quick brown fox jumps 样式继承"
        spans = [(0, 5, {"size": 20, "bold": True}),
                 (10, 20, {"color": "red", "italic": True})]
        doc.append({"style": names[i % len(names)],
                    "props": {"color": rng.choice(["black", "blue"])},
                    "text": text, "spans": spans})
    return styles, doc


def main():
    styles, doc = build()
    total_chars = sum(len(p["text"]) for p in doc)

    tracemalloc.start()
    t0 = time.perf_counter()
    table = expand_document(StyleSheet(styles), doc)
    elapsed = time.perf_counter() - t0
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    n_attrs = sum(len(row) for para in table for row in para)
    line = (
        f"段落数: {N_PARAS}\n"
        f"字符总数: {total_chars}\n"
        f"字符x属性记录数: {n_attrs}\n"
        f"展开耗时: {elapsed*1000:.1f} ms\n"
        f"速度: {total_chars/elapsed/1000:.0f} 万字符/秒\n"
        f"Python 对象内存峰值(tracemalloc): {peak/1024/1024:.1f} MiB\n"
        f"进程 RSS 峰值(ru_maxrss): {rss_kb/1024:.1f} MiB\n"
    )
    print(line)
    out = os.path.join(os.path.dirname(__file__), "out", "bench.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(line)


if __name__ == "__main__":
    main()
