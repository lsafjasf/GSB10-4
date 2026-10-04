"""时区与精度统一前后的差异演示，输出 data/normalize_diff.txt。"""

from interval_merge import Interval, format_us, merge_inputs, merge_sorted, quantize

LINES = []
def out(s=""):
    print(s)
    LINES.append(s)

out("== 时区统一前后 ==")
raw = [
    ("2026-01-01T08:00:00+08:00", "2026-01-01T09:00:00+08:00"),  # UTC 00:00-01:00
    ("2026-01-01T00:30:00Z", "2026-01-01T01:30:00Z"),            # UTC 00:30-01:30
]
out("原始输入（两个区间物理上重叠 00:30-01:00 UTC）:")
for a, b in raw:
    out(f"  [{a}, {b}]")
rep = merge_inputs(raw)
out("统一为 UTC 后合并结果:")
for iv in rep.merged:
    out(f"  [{format_us(iv.start)}, {format_us(iv.end)}]  (1 段，正确识别重叠)")
out("若不统一时区直接按字符串比较，08:00+08:00 与 00:30Z 会被误判为不相交 -> 2 段（错误）")
out()

out("== 精度统一前后 ==")
a = Interval(0, 999_999)          # [0s, 0.999999s]
b = Interval(1_000_000, 2_000_000)  # [1s, 2s]
out(f"原始区间: [{a.start}, {a.end}]us 与 [{b.start}, {b.end}]us （相差 1us，不相接）")
out(f"统一前合并: {len(merge_sorted([a, b]))} 段")
qa, qb = quantize(a, 1_000_000), quantize(b, 1_000_000)
out(f"按 1 秒量子对齐: [{qa.start}, {qa.end}]us 与 [{qb.start}, {qb.end}]us")
out(f"统一后合并: {[ (iv.start, iv.end) for iv in merge_sorted([qa, qb]) ]} （相接，合并为 1 段）")
out("规则: start 向下取整、end 向上取整，对齐只扩不缩，不丢覆盖")

with open("data/normalize_diff.txt", "w") as f:
    f.write("\n".join(LINES) + "\n")
