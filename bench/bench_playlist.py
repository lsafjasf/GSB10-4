"""20 万条路径的解析+去重压测，并与朴素字符串去重对比。

用法: python3 bench/bench_playlist.py [--size 200000] [--seed 42]
输出: 控制台报告 + docs/bench_results.json
"""

import argparse
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playlist_dedup import dedup_playlist, naive_string_dedup  # noqa: E402


def build_corpus(root: Path, n_files: int) -> dict:
    music = root / "music"
    subdirs = [music / name for name in ("rock", "pop", "jazz", "live")]
    for d in subdirs:
        d.mkdir(parents=True)
    files = []
    for i in range(n_files):
        p = subdirs[i % len(subdirs)] / f"track_{i:05d}.mp3"
        p.write_bytes(b"x")
        files.append(p)
    # 目录符号链接别名
    os.symlink(music, root / "music_alias")
    # 一批文件级符号链接别名
    alias_dir = root / "aliases"
    alias_dir.mkdir()
    file_aliases = []
    for i in range(0, n_files, 10):
        a = alias_dir / f"alias_{i:05d}.mp3"
        os.symlink(files[i], a)
        file_aliases.append(a)
    # 一批硬链接
    hard_dir = root / "hardlinks"
    hard_dir.mkdir()
    hardlinks = []
    for i in range(5, n_files, 10):
        h = hard_dir / f"hard_{i:05d}.mp3"
        os.link(files[i], h)
        hardlinks.append(h)
    # 符号链接成环
    os.symlink(root / "loop_b", root / "loop_a")
    os.symlink(root / "loop_a", root / "loop_b")
    # 权限不足目录
    locked = root / "locked"
    locked.mkdir()
    (locked / "secret.mp3").write_bytes(b"x")
    locked.chmod(0o000)
    return {
        "files": files,
        "file_aliases": file_aliases,
        "hardlinks": hardlinks,
        "locked": locked,
    }


def gen_playlist(root: Path, corpus: dict, size: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    files = corpus["files"]
    aliases = corpus["file_aliases"]
    hards = corpus["hardlinks"]
    items = []
    for _ in range(size):
        r = rng.random()
        f = rng.choice(files)
        rel = f.relative_to(root)
        if r < 0.30:      # 绝对规范路径
            items.append(str(f))
        elif r < 0.50:    # 相对路径
            items.append(str(rel))
        elif r < 0.62:    # 含 ./ 或 ../ 的写法
            parts = list(rel.parts)
            if rng.random() < 0.5:
                items.append(str(Path(parts[0], ".", *parts[1:])))
            else:
                items.append(str(Path(parts[0], "jazz", "..", *parts[1:])))
        elif r < 0.74:    # 绝对路径含 . / ..
            items.append(str(root / "music" / "jazz" / ".." / Path(*rel.parts[1:])))
        elif r < 0.84:    # 目录符号链接别名
            items.append(str(root / "music_alias" / Path(*rel.parts[1:])))
        elif r < 0.89:    # 文件级符号链接
            items.append(str(rng.choice(aliases)))
        elif r < 0.94:    # 硬链接
            items.append(str(rng.choice(hards)))
        elif r < 0.97:    # 不存在的路径
            items.append(str(root / "music" / "rock" / f"ghost_{rng.randrange(10**6)}.mp3"))
        elif r < 0.99:    # 符号链接成环
            items.append(str(root / "loop_a" / "x.mp3"))
        else:             # 权限不足
            items.append(str(corpus["locked"] / "secret.mp3"))
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=200_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--files", type=int, default=2_000)
    args = ap.parse_args()

    tmp = tempfile.TemporaryDirectory(prefix="plbench_")
    root = Path(tmp.name)
    corpus = build_corpus(root, args.files)
    items = gen_playlist(root, corpus, args.size, args.seed)
    base = str(root)

    t0 = time.perf_counter()
    result = dedup_playlist(items, base_dir=base)
    t1 = time.perf_counter()
    real_seconds = t1 - t0

    t2 = time.perf_counter()
    naive_unique, naive_dups = naive_string_dedup(items)
    t3 = time.perf_counter()
    naive_seconds = t3 - t2

    real_unique = len(result.unique)
    naive_unique_count = len(naive_unique)
    diff = naive_unique_count - real_unique

    # 精确拆解朴素去重"多报的唯一条目"：
    # 按字符串首次出现取下标，再看该下标在真实文件去重里的归宿。
    first_idx_by_str = {}
    for idx, raw in enumerate(items):
        first_idx_by_str.setdefault(raw, idx)
    by_index = {}
    for group in (result.unique, result.duplicates, result.errors):
        for entry in group:
            by_index[entry.index] = entry
    missed_duplicates = 0   # 实为重复，朴素法却当成唯一
    errors_as_unique = 0    # 无法解析（不存在/成环/权限），朴素法当成唯一
    for raw in naive_unique:
        entry = by_index[first_idx_by_str[raw]]
        if entry.is_duplicate:
            missed_duplicates += 1
        elif entry.is_error:
            errors_as_unique += 1

    report = {
        "size": len(items),
        "seed": args.seed,
        "real_files_on_disk": args.files,
        "real_file_dedup": {
            "seconds": round(real_seconds, 4),
            "unique": real_unique,
            "duplicates": len(result.duplicates),
            "errors": len(result.errors),
            "error_counts": result.error_counts(),
            "per_100k_seconds": round(real_seconds / len(items) * 100_000, 4),
        },
        "naive_string_dedup": {
            "seconds": round(naive_seconds, 4),
            "unique": naive_unique_count,
            "duplicates": len(naive_dups),
        },
        "diff_unique_count": diff,
        "diff_breakdown": {
            "missed_duplicates": missed_duplicates,
            "errors_counted_as_unique": errors_as_unique,
        },
        "note": "diff = 朴素字符串去重多报的唯一条目数 = 漏判的重复 + 被当作唯一的错误路径",
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))

    out = Path(__file__).resolve().parent.parent / "docs" / "bench_results.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    corpus["locked"].chmod(0o700)
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
