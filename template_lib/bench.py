"""10 万次替换的耗时与内存峰值基准（仅标准库）。

耗时测量与内存测量分两轮独立进行: tracemalloc 会显著拖慢分配，
所以正式耗时不启用跟踪；内存峰值在单独一轮用 tracemalloc 采样。

运行: python3 bench.py [次数]
"""

import gc
import os
import resource
import sys
import time
import tracemalloc

from template_engine import render

TEMPLATE = (
    "用户 {{user}} 于 {{ts}} 提交订单 {{order_id}}，"
    r"金额 {{amount}} 元，转义 \{{literal}} 保留，"
    "详情: {{detail}}，结尾"
)
VARIABLES = {
    "user": "alice",
    "ts": "2026-10-05T10:00:00+08:00",
    "order_id": "ORD-20261005-0001",
    "amount": "128.50",
    # 变量值内含一层嵌套引用
    "detail": "商品={{item}}, 渠道={{channel}}",
    "item": "mechanical-keyboard",
    "channel": "web",
}


def run(times: int) -> None:
    expected = render(TEMPLATE, VARIABLES)

    # ---- 第 1 轮: 纯耗时（tracemalloc 关闭） ----
    gc.collect()
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()
    for _ in range(times):
        out = render(TEMPLATE, VARIABLES)
        assert out == expected
    elapsed = time.perf_counter() - t0

    # ---- 第 2 轮: Python 堆内存峰值（tracemalloc） ----
    tracemalloc.start()
    for _ in range(min(times, 10_000)):
        out = render(TEMPLATE, VARIABLES)
    _current, peak_py = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    print(f"运行环境      : Python {sys.version.split()[0]}, {sys.platform}")
    print(f"替换次数      : {times:,}")
    print(f"总耗时        : {elapsed:.4f} s")
    print(f"平均每次      : {elapsed / times * 1e6:.3f} µs")
    print(f"吞吐          : {times / elapsed:,.0f} 次/秒")
    print(f"Python 堆峰值 : {peak_py / 1024:.1f} KiB "
          f"(tracemalloc, 单次渲染在堆上的工作集)")
    print(f"进程 RSS 峰值 : {max(rss_after - rss_before, 0) / 1024:.2f} MiB 增量, "
          f"{rss_after / 1024:.1f} MiB 总量 (ru_maxrss)")
    print(f"输出长度      : {len(expected)} 字符")
    print("-" * 60)
    print("单次渲染结果:", expected)


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 100_000)
