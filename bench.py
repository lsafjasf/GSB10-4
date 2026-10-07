"""基准: 1000 条目 / 500MB 包体的局部替换性能。
用法: python3 bench.py [--keep]   结束后默认删除大包, --keep 则保留。
"""
import argparse
import json
import os
import shutil
import time

import respack

N_ENTRIES = 1000
ENTRY_SIZE = 500_000                       # 1000 * 500_000 = 500,000,000 B
WORKDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench_data")
PACK = os.path.join(WORKDIR, "big.rpk")
SNAP = os.path.join(WORKDIR, "snapshot.rpk")
RESULT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench_result.json")
CHUNK = 1 << 20


def chunk_factory(seed: int, length: int):
    def gen():
        block = bytes((i + seed) & 0xFF for i in range(CHUNK))
        remaining = length
        while remaining > 0:
            n = min(CHUNK, remaining)
            yield block[:n]
            remaining -= n
    return gen


def write_bytes_io() -> int:
    try:
        with open("/proc/self/io") as f:
            for line in f:
                if line.startswith("write_bytes:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return -1


def main(keep: bool):
    os.makedirs(WORKDIR, exist_ok=True)
    names = [f"entry_{i:04d}.bin" for i in range(N_ENTRIES)]

    print(f"构建资源包: {N_ENTRIES} 条目, 数据区 {N_ENTRIES * ENTRY_SIZE / 1e6:.0f} MB ...")
    t0 = time.perf_counter()
    respack.build_pack(PACK, [(n, (ENTRY_SIZE, chunk_factory(i, ENTRY_SIZE)))
                              for i, n in enumerate(names)])
    build_sec = time.perf_counter() - t0
    pack_size = os.path.getsize(PACK)

    print(f"构建耗时 {build_sec:.2f}s, 文件 {pack_size / 1e6:.1f} MB; 复制基线快照 ...")
    t0 = time.perf_counter()
    shutil.copyfile(PACK, SNAP)
    copy_sec = time.perf_counter() - t0
    print(f"快照复制 {copy_sec:.2f}s")

    scenarios = [
        ("append_grow_mid",   names[500], b"M" * 600_000, "append"),
        ("append_shrink_first", names[0], b"S" * 50_000,  "append"),
        ("inplace_shrink_last", names[-1], b"L" * 400_000, "inplace"),
        ("inplace_equal_mid", names[250], b"E" * ENTRY_SIZE, "inplace"),
    ]

    results = []
    for tag, entry, data, strategy in scenarios:
        shutil.copyfile(SNAP, PACK)
        io0 = write_bytes_io()
        rep = respack.replace(PACK, entry, data, strategy)
        io1 = write_bytes_io()
        size_delta = os.path.getsize(PACK) - pack_size

        t0 = time.perf_counter()
        info = respack.verify_others_unchanged(SNAP, PACK, entry)
        verify_sec = time.perf_counter() - t0

        row = {
            "scenario": tag,
            "entry": entry,
            "strategy": strategy,
            "old_length": rep.old_length,
            "new_length": rep.new_length,
            "old_offset": rep.old_offset,
            "new_offset": rep.new_offset,
            "replace_sec": round(rep.elapsed_sec, 4),
            "library_bytes_written": rep.bytes_written,
            "proc_write_bytes_delta": io1 - io0 if io0 >= 0 else None,
            "file_size_delta": size_delta,
            "verify_sec": round(verify_sec, 3),
            "entries_verified_byte_identical": info["entries_checked"],
            "diff_regions": info["regions"],
        }
        results.append(row)
        print(f"[{tag}] {strategy:7s} {entry}: {rep.elapsed_sec*1000:7.1f} ms, "
              f"写入 {rep.bytes_written/1024:.1f} KiB, 大小增量 {size_delta:+d} B, "
              f"差异区间 {len(info['regions'])} 段, 校验 {info['entries_checked']} 条目")

    out = {
        "entry_count": N_ENTRIES,
        "data_bytes": N_ENTRIES * ENTRY_SIZE,
        "pack_size": pack_size,
        "build_sec": round(build_sec, 3),
        "snapshot_copy_sec": round(copy_sec, 3),
        "scenarios": results,
    }
    with open(RESULT_JSON, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"结果(含差异定位数据) -> {RESULT_JSON}")

    if not keep:
        shutil.rmtree(WORKDIR)
        print("已清理 bench_data/ (加 --keep 可保留大包)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    main(ap.parse_args().keep)
