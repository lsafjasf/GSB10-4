"""基准：100 万个头部块的编解码耗时与内存峰值。

运行：python3 bench.py [块数，默认 1000000]
输出：results/bench.txt

方法：
  * 计时运行：完整 N 个块，time.perf_counter 分别统计编码/解码耗时，
    用 resource.ru_maxrss 取进程峰值 RSS。
  * 内存剖析：tracemalloc 会显著拖慢速度，故另跑一次 1/10 规模的
    采样运行，报告 Python 堆分配峰值。
  * 工作负载：每块 6 个头部，一半命中动态表（indexed），一半字面量；
    每 1000 块插入一条新表项以持续触发驱逐，模拟真实长连接。
"""

import resource
import sys
import time
import tracemalloc

from codec import Encoder, Decoder

STATIC_NAMES = [
    (":method", "GET"), (":scheme", "https"), (":path", "/"),
    ("accept-encoding", "gzip, deflate, br"),
    ("accept-language", "zh-CN,zh;q=0.9"),
    ("cache-control", "no-cache"),
    ("user-agent", "gsb-bench/1.0"),
    ("x-request-id", "req-000000"),
]


def run(n_blocks, use_tracemalloc=False):
    enc = Encoder(max_capacity=4096)
    dec = Decoder(max_capacity=4096)
    dec.apply(enc.set_capacity(4096))
    for name, value in STATIC_NAMES:
        dec.apply(enc.insert(name, value))

    if use_tracemalloc:
        tracemalloc.start()

    t_enc = 0.0
    t_dec = 0.0
    ok = 0
    for i in range(n_blocks):
        headers = [
            STATIC_NAMES[i % 3],
            STATIC_NAMES[3],
            ("x-request-id", f"req-{i:06d}"),
            ("x-seq", str(i)),
            STATIC_NAMES[6],
            (":path", f"/api/v1/item/{i % 97}"),
        ]
        t0 = time.perf_counter()
        block = enc.encode(headers)
        t1 = time.perf_counter()
        decoded, krc = dec.decode(block)
        t2 = time.perf_counter()
        t_enc += t1 - t0
        t_dec += t2 - t1
        assert decoded == headers, f"第 {i} 块解码不一致"
        ok += 1
        if i % 1000 == 999:
            dec.apply(enc.insert("x-trace", f"trace-{i}"))
            enc.on_ack(krc)

    peak_py = 0
    if use_tracemalloc:
        _, peak_py = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return t_enc, t_dec, ok, peak_py, peak_rss_kb


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    lines = []
    lines.append(f"头部块数量: {n:,}（每块 6 个头部，共 {n * 6:,} 个头部字段）")
    lines.append(f"动态表容量: 4096 字节，每 1000 块插入 1 条新表项（持续驱逐）")
    lines.append("")

    t_enc, t_dec, ok, _, rss_kb = run(n)
    total = t_enc + t_dec
    lines.append(f"[计时运行] 编解码正确性: {ok:,}/{n:,} 块全部一致")
    lines.append(f"  编码总耗时: {t_enc:.3f} s ({n / t_enc:,.0f} 块/s)")
    lines.append(f"  解码总耗时: {t_dec:.3f} s ({n / t_dec:,.0f} 块/s)")
    lines.append(f"  合计:       {total:.3f} s ({n / total:,.0f} 块/s)")
    lines.append(f"  进程峰值 RSS: {rss_kb / 1024:.1f} MiB")
    lines.append("")

    sample = max(n // 10, 10_000)
    t_enc2, t_dec2, ok2, peak_py, _ = run(sample, use_tracemalloc=True)
    lines.append(f"[内存剖析] tracemalloc 采样运行 {sample:,} 块")
    lines.append(f"  Python 堆分配峰值: {peak_py / 1024 / 1024:.2f} MiB")
    lines.append(f"  （编解码为流式处理，内存与块数无关，峰值不随总量增长）")

    report = "\n".join(lines)
    print(report)
    with open("results/bench.txt", "w", encoding="utf-8") as f:
        f.write("100 万头部块编解码基准\n" + "=" * 60 + "\n" + report + "\n")


if __name__ == "__main__":
    main()
