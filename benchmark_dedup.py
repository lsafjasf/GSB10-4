"""基准:20 万条播放列表路径的解析/去重耗时,及与朴素字符串去重的差异。

数据:50000 个真实空文件,每个文件 4 种写法(相对路径、绝对路径、
含 "." 与重复分隔符的词法写法、经符号链接目录的写法),固定种子乱序。
运行:python3 benchmark_dedup.py
"""

import json
import os
import platform
import random
import shutil
import sys
import tempfile
import time
from statistics import median

from playlist_dedup import Resolver, dedup_entries, naive_string_dedup

N_FILES = 50_000
N_ENTRIES = 200_000
ROUNDS = 3
SEED = 20261005


def build_tree(root: str) -> tuple:
    music = os.path.join(root, "music")
    os.makedirs(music)
    for i in range(N_FILES):
        fd = os.open(os.path.join(music, f"f{i}.dat"),
                     os.O_WRONLY | os.O_CREAT, 0o644)
        os.close(fd)
    link_dir = os.path.join(root, "via_link")
    os.symlink("music", link_dir)
    return music, link_dir


def build_entries(music: str, link_dir: str) -> list:
    entries = []
    for i in range(N_FILES):
        name = f"f{i}.dat"
        entries.append((name, music))                                       # 相对
        entries.append((os.path.join(music, name), None))                  # 绝对
        entries.append((music + os.sep + os.sep + "." + os.sep + name, None))  # 词法
        entries.append((os.path.join(link_dir, name), None))               # 链接目录
    random.Random(SEED).shuffle(entries)
    assert len(entries) == N_ENTRIES
    return entries


def per_entry_disagreement(entries: list, result) -> int:
    """逐条目统计两种方法判定不一致的条数。

    朴素:原始字符串是否首现;规范:(dev,inode) 是否首现。
    """
    seen_str, disagree = set(), 0
    canonical_dup = {d.index for d in result.duplicates}
    error_idx = {e.index for e in result.errors}
    for i, (path, _) in enumerate(entries):
        naive_dup = path in seen_str
        seen_str.add(path)
        canon_dup = i in canonical_dup
        if i not in error_idx and naive_dup != canon_dup:
            disagree += 1
    return disagree


def main() -> int:
    root = tempfile.mkdtemp(prefix="pdbench_")
    try:
        t0 = time.perf_counter()
        music, link_dir = build_tree(root)
        build_seconds = time.perf_counter() - t0

        entries = build_entries(music, link_dir)

        # ---- 朴素字符串去重基线(取最佳一轮) ----
        naive_times = []
        for _ in range(ROUNDS):
            t0 = time.perf_counter()
            naive_kept = naive_string_dedup(entries)
            naive_times.append(time.perf_counter() - t0)

        # ---- 规范解析+去重(每轮新建 Resolver,不复用系统调用缓存) ----
        canonical_times = []
        last_result = None
        for _ in range(ROUNDS):
            resolver = Resolver()
            t0 = time.perf_counter()
            result = dedup_entries(entries, resolver=resolver)
            canonical_times.append(time.perf_counter() - t0)
            last_result = result

        disagree = per_entry_disagreement(entries, last_result)
        first_form = {}
        for u in last_result.unique:
            kind = ("relative" if not os.path.isabs(u.path)
                    else "via-link" if link_dir in u.path
                    else "lexical" if os.sep + "." + os.sep in u.path
                    else "absolute")
            first_form[kind] = first_form.get(kind, 0) + 1

        report = {
            "input_entries": N_ENTRIES,
            "real_files": N_FILES,
            "build_tree_seconds": round(build_seconds, 3),
            "canonical": {
                "unique": len(last_result.unique),
                "duplicates": len(last_result.duplicates),
                "errors": len(last_result.errors),
                "min_seconds": round(min(canonical_times), 4),
                "median_seconds": round(median(canonical_times), 4),
                "throughput_entries_per_sec": round(
                    N_ENTRIES / median(canonical_times)),
                "first_occurrence_form_distribution": first_form,
            },
            "naive_string": {
                "unique": len(naive_kept),
                "min_seconds": round(min(naive_times), 4),
                "median_seconds": round(median(naive_times), 4),
            },
            "difference": {
                "unique_count_gap": len(naive_kept) - len(last_result.unique),
                "per_entry_judgment_disagreements": disagree,
                "note": "朴素按原始字符串判重,4 种写法被当成 4 个文件;"
                        "规范按 (dev,inode) 判重,收敛为 1 个文件。",
            },
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "seed": SEED,
                "rounds": ROUNDS,
            },
        }
        with open("benchmark_results.json", "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)

        c = report["canonical"]
        n = report["naive_string"]
        print("20 万条路径基准结果")
        print(f"  规范去重: unique={c['unique']} duplicates={c['duplicates']} "
              f"errors={c['errors']}")
        print(f"  耗时: min={c['min_seconds']}s median={c['median_seconds']}s "
              f"吞吐={c['throughput_entries_per_sec']} 条/秒")
        print(f"  朴素字符串去重: unique={n['unique']} "
              f"min={n['min_seconds']}s median={n['median_seconds']}s")
        print(f"  差异: 首现集合相差 {report['difference']['unique_count_gap']} 条,"
              f"逐条目判定不一致 {disagree} 条")
        print("  明细已写入 benchmark_results.json")
        return 0
    finally:
        shutil.rmtree(root, True)


if __name__ == "__main__":
    raise SystemExit(main())
