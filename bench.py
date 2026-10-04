"""10 万次替换基准：耗时与内存峰值（仅标准库 tracemalloc）。

运行: python3 bench.py [迭代次数，默认 100000]
"""
import os
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tplfill import render, Raw

N = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000

SCENARIOS = [
    ("平铺多占位符",
     "用户 {{name}} <{{email}}> 于 {{date}} 完成 {{action}}，余额 {{amount}} 元。",
     {"name": "张三", "email": "z@example.com", "date": "2026-10-05",
      "action": "下单", "amount": "128.50"}),
    ("三层嵌套",
     "{{title}}: {{body}}",
     {"title": "通知", "body": "{{greet}}，{{user}}",
      "greet": "你好", "user": "{{name}}", "name": Raw("{{evil}}")}),
    ("不可信用户内容(含花括号)",
     "评论: {{comment}} | 长度={{len}}",
     {"comment": Raw("{{injected}} \\{ x } {{{ "), "len": 19}),
    ("无占位符长文本",
     "这是一段没有任何占位符的纯文本，含 { 单括号 } 与 \\{转义\\}，原样输出。" * 4,
     {"a": "A"}),
]


def bench(label, template, variables, n):
    render(template, variables)  # 预热

    t0 = time.perf_counter()
    sink = ""
    for _ in range(n):
        sink = render(template, variables)
    elapsed = time.perf_counter() - t0

    tracemalloc.start()
    render(template, variables)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 单次内存：每次循环新增分配的近似值（统计 n 次总分配）
    tracemalloc.start()
    for _ in range(n):
        sink2 = render(template, variables)
    current_allocated, peak_loop = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print("%-28s %8d 次  %8.3f s  %9.0f ns/次  稳态峰值 %6.2f KiB  "
          "循环内峰值 %6.2f KiB"
          % (label, n, elapsed, elapsed / n * 1e9, peak / 1024,
             peak_loop / 1024))
    return sink, sink2


def main():
    print("Python %s | 迭代次数 %d" % (sys.version.split()[0], N))
    print("-" * 92)
    for label, template, variables in SCENARIOS:
        bench(label, template, variables, N)
    print("-" * 92)
    print("说明: 稳态峰值为一次独立 render 的 tracemalloc 峰值；"
          "循环内峰值为连续 %d 次渲染期间的峰值。" % N)


if __name__ == "__main__":
    main()
