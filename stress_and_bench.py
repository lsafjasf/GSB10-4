"""并发压测 + 吞吐/内存基准（python3 stress_and_bench.py）。

- 并发：单生产者/单消费者穿过小环形缓冲区，每字节都带全局偏移校验，
  任何覆盖未读数据、错位、丢字节都会立刻报错；另做 4 对线程的多轮压测。
- 基准：100 万次写 + 100 万次读，统计吞吐；tracemalloc / ru_maxrss 统计内存。
结果落盘 concurrency_result.txt 与 throughput.txt。
"""

import os
import random
import resource
import sys
import threading
import time
import tracemalloc

from ring_buffer import RingBuffer

HERE = os.path.dirname(os.path.abspath(__file__))


def stream_byte(offset: int) -> int:
    """确定性字节流 f(offset)：任意错位都会被发现。"""
    return (offset * 2654435761 + 7) & 0xFF


def chunk(offset: int, n: int) -> bytes:
    return bytes(stream_byte(offset + i) for i in range(n))


def run_spsc_round(total_bytes, cap, seed, max_chunk_w, max_chunk_r,
                   stats, stop_evt):
    rb = RingBuffer(cap)
    prng_w = random.Random(seed)
    prng_r = random.Random(seed + 1)
    errors = []

    def producer():
        offset = 0
        try:
            while offset < total_bytes:
                n = min(prng_w.randint(1, max_chunk_w), total_bytes - offset)
                rb.write_blocking(chunk(offset, n))
                offset += n
                if stop_evt.is_set():
                    break
        except Exception as exc:  # pragma: no cover
            errors.append(f"producer: {exc!r}")

    def consumer():
        offset = 0
        try:
            while offset < total_bytes:
                n = min(prng_r.randint(1, max_chunk_r), total_bytes - offset)
                data = rb.read_blocking(n, timeout=10)
                if not data:
                    raise RuntimeError("consumer timeout")
                # 整块按全局偏移比对（任何覆盖/错位都会使字节不等）
                if data != chunk(offset, len(data)):
                    for i, b in enumerate(data):
                        if b != stream_byte(offset + i):
                            raise AssertionError(
                                f"mismatch at {offset + i}: got {b} "
                                f"want {stream_byte(offset + i)} (覆盖/错位)")
                offset += len(data)
            assert rb.is_empty()
        except Exception as exc:  # pragma: no cover
            errors.append(f"consumer: {exc!r}")
            stop_evt.set()

    t0 = time.perf_counter()
    tp = threading.Thread(target=producer)
    tc = threading.Thread(target=consumer)
    tp.start(); tc.start()
    tp.join(); tc.join()
    elapsed = time.perf_counter() - t0
    stats["bytes"] = total_bytes
    stats["elapsed"] = elapsed
    stats["ok"] = not errors
    stats["errors"] = errors


def concurrency_stress():
    lines = ["生产/消费并发压测结果",
             "=" * 60,
             "校验方式: 消费者对每个字节按全局偏移核对 f(i)=(i*2654435761+7)&0xFF;",
             "          任何覆盖未读数据/错位/丢失都会触发 AssertionError。",
             ""]
    all_ok = True

    rounds = [
        # total, cap, seed, max_w, max_r
        (8 * 1024 * 1024, 4096, 1001, 700, 500),
        (8 * 1024 * 1024, 1024, 1002, 4096, 333),   # 写块 > 容量，必走分段
        (4 * 1024 * 1024, 17,   1003, 50,  13),     # 极小奇容量，回绕极频繁
    ]
    for idx, (total, cap, seed, mw, mr) in enumerate(rounds, 1):
        stop = threading.Event()
        stats = {}
        run_spsc_round(total, cap, seed, mw, mr, stats, stop)
        all_ok &= stats["ok"]
        lines.append(
            f"round{idx}: total={total/1024/1024:.1f}MiB cap={cap} "
            f"write<= {mw}B read<= {mr}B "
            f"-> {'PASS' if stats['ok'] else 'FAIL'} "
            f"({stats['elapsed']:.3f}s, "
            f"{total/stats['elapsed']/1024/1024:.1f} MiB/s)")
        for err in stats["errors"]:
            lines.append(f"    {err}")

    lines.append("")
    lines.append("multi-pair: 4 个独立 RingBuffer x (生产者+消费者), 每对 2 MiB")
    threads = []
    results = [None] * 4

    def worker(i):
        stop = threading.Event()
        stats = {}
        run_spsc_round(2 * 1024 * 1024, 512, 9000 + i, 300, 127, stats, stop)
        results[i] = stats

    for i in range(4):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t); t.start()
    for t in threads:
        t.join()
    for i, stats in enumerate(results):
        all_ok &= stats["ok"]
        lines.append(f"    pair{i}: {'PASS' if stats['ok'] else 'FAIL'} "
                     f"({stats['elapsed']:.3f}s)")
    lines.append("")
    lines.append("OVERALL: " + ("PASS - 未读数据从未被覆盖，字节流零错位零丢失"
                              if all_ok else "FAIL"))
    text = "\n".join(lines) + "\n"
    with open(os.path.join(HERE, "concurrency_result.txt"), "w",
              encoding="utf-8") as f:
        f.write(text)
    print(text)
    if not all_ok:
        sys.exit(1)


def throughput_bench():
    ops = 1_000_000
    cap = 4096
    rb = RingBuffer(cap)
    rng = random.Random(42)
    chunks = [chunk(i, i % 256 + 1) for i in range(256)]  # 预生成，剔除造数开销

    tracemalloc.start()
    t0 = time.perf_counter()
    bytes_total = 0
    for _ in range(ops):
        data = chunks[rng.randrange(256)]
        written = rb.write(data)
        got = rb.read(written)
        assert got == data
        bytes_total += written
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    buf_bytes = sys.getsizeof(rb._buf) + sys.getsizeof(rb)
    lines = [
        "100 万次读写吞吐 / 内存数据",
        "=" * 60,
        f"操作: write + read 配对 {ops:,} 次（块大小 1..256 字节, cap=4096）",
        f"总搬运: {bytes_total:,} 字节 ({bytes_total/1024/1024:.2f} MiB)",
        f"耗时: {elapsed:.4f} s",
        f"吞吐: {ops/elapsed:,.0f} ops/s "
        f"(每次写或读平均 {elapsed/ops*1e6:.3f} us)",
        f"带宽: {bytes_total/elapsed/1024/1024:.1f} MiB/s",
        "",
        "内存占用:",
        f"  RingBuffer 本体(bytearray 存储 + 对象): {buf_bytes} 字节",
        f"  tracemalloc 基准峰值: {peak/1024:.1f} KiB "
        f"(含 256 个预生成数据块 {sum(map(len, chunks))} 字节)",
        f"  进程 ru_maxrss(峰值常驻, 含解释器): {rss_kb/1024:.1f} MiB",
    ]
    text = "\n".join(lines) + "\n"
    with open(os.path.join(HERE, "throughput.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    concurrency_stress()
    throughput_bench()
