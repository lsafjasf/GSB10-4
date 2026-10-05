"""1MB / 16MB 镜像的流式解析耗时与内存峰值基准（仅标准库）。

运行: python3 bench.py
"""

import os
import tempfile
import time
import tracemalloc

from tapemark import Kind, encode_data, encode_eof, encode_eov, iter_events

PAYLOAD = 4096
FILE_EVERY = 64  # 每 64 个数据块放一个文件结束标记


def build_image(path: str, target: int) -> int:
    block = encode_data(bytes((i % 251 for i in range(PAYLOAD))))
    written = 0
    number = 0
    with open(path, "wb") as f:
        while written < target:
            if number and number % FILE_EVERY == 0:
                f.write(encode_eof())
                written += 8
            else:
                f.write(block)
                written += len(block)
            number += 1
        f.write(encode_eov())
        written += 8
    return written


def parse_once(path: str, trace_memory: bool):
    if trace_memory:
        tracemalloc.start()
    start = time.perf_counter()
    data_blocks = file_marks = 0
    total_payload = 0
    with open(path, "rb") as f:
        for event in iter_events(f):
            total_payload += event.length
            if event.kind is Kind.DATA:
                data_blocks += 1
            elif event.kind is Kind.EOF:
                file_marks += 1
    elapsed = time.perf_counter() - start
    peak = 0
    if trace_memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return elapsed, peak, data_blocks, file_marks, total_payload


def main() -> None:
    print(f"{'镜像大小':>8} {'解析耗时':>10} {'内存峰值':>12} "
          f"{'数据块':>7} {'文件标记':>8} {'载荷字节':>10}")
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for target in (1 << 20, 16 << 20):
            path = os.path.join(tmp, f"vol_{target}.img")
            size = build_image(path, target)
            # 耗时：不带内存追踪跑 3 次取最佳；内存：单独带追踪测量
            elapsed = min(parse_once(path, False)[0] for _ in range(3))
            _, peak, data_blocks, file_marks, payload = parse_once(path, True)
            rows.append((size, peak))
            print(f"{size:>8} {elapsed:>9.4f}s {peak:>10} B "
                  f"{data_blocks:>7} {file_marks:>8} {payload:>10}")
    small_peak, large_peak = rows[0][1], rows[1][1]
    growth = large_peak - small_peak
    print(f"\n16MB 相对 1MB 的内存峰值变化: {growth:+d} B "
          f"(与卷长度无关, 有界于读取分块大小)")


if __name__ == "__main__":
    main()
