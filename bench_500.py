"""500 页演示文稿：解析耗时 + 递归深度上界实测。"""
import json
import sys
import threading
import time

from slide_order import analyze

with open("examples/deck500.json", encoding="utf-8") as fh:
    spec = json.load(fh)

# 用极小的递归限制证明算法不依赖递归：若实现含递归，这里会直接 RecursionError。
old_limit = sys.getrecursionlimit()
sys.setrecursionlimit(80)
try:
    begin = time.perf_counter()
    result = analyze(spec)
    elapsed_ms = (time.perf_counter() - begin) * 1000.0
finally:
    sys.setrecursionlimit(old_limit)

print(f"页数: 500")
print(f"解析耗时: {elapsed_ms:.3f} ms（库内计时 {result.elapsed_ms:.3f} ms）")
print(f"遍历链最长步数: {result.max_chain_depth}")
print(f"递归深度上界: {result.call_stack_depth_bound}（纯迭代实现；"
      f"在 recursionlimit=80 下跑通 500 页链即为实证）")
print(f"推导序列长度: {len(result.order)}，异常数: {len(result.anomalies)}")
mismatch = sum(1 for c in result.comparison if c.status == "mismatch")
print(f"与按编号排序不一致的页数: {mismatch}")
