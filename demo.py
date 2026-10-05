"""逐行对拍数据打印：并列值并入/截断、空帧、全帧、单行、重复运行一致性。

运行: python3 demo.py
"""

import hashlib

from framecalc import (
    Bound, BoundType, FrameMode, FrameSpec,
    compute_frames as _compute_frames, sort_order, window_aggregate,
)


def compute_frames(spec, sorted_keys):
    los, his = _compute_frames(spec, sorted_keys)
    return list(zip(los, his))

UBP = Bound(BoundType.UNBOUNDED_PRECEDING)
UBF = Bound(BoundType.UNBOUNDED_FOLLOWING)
CUR = Bound(BoundType.CURRENT_ROW)


def P(n):
    return Bound(BoundType.PRECEDING, n)


def F(n):
    return Bound(BoundType.FOLLOWING, n)


def show(title, spec, keys, vals):
    order = sort_order(keys)
    skeys = [keys[i] for i in order]
    svals = [vals[i] for i in order]
    frames = compute_frames(spec, skeys)
    sums = window_aggregate(spec, keys, vals, "sum")
    counts = window_aggregate(spec, keys, vals, "count")
    print("=" * 78)
    print(title)
    print("规格: %s BETWEEN %s AND %s"
          % (spec.mode.value, _fmt(spec.start), _fmt(spec.end)))
    print("排序后键: %s   排序后值: %s" % (skeys, svals))
    print("%-4s %-6s %-8s %-8s %-10s %-10s"
          % ("行", "键", "值", "帧[lo,hi]", "帧内值", "sum/count"))
    for pos, idx in enumerate(order):
        lo, hi = frames[pos]
        if lo > hi:
            members, s = "(空帧)", None
        else:
            members = str(svals[lo:hi + 1])
            s = sum(svals[lo:hi + 1])
        print("%-4d %-6r %-8r %-8s %-10s sum=%-6s count=%d"
              % (pos, skeys[pos], svals[pos],
                 "[%d,%d]" % (lo, hi) if lo <= hi else "(%d,%d)" % (lo, hi),
                 members, s, counts[idx]))
    print("按原始行号返回的 sum:   %s" % sums)
    print("按原始行号返回的 count: %s" % counts)


def _fmt(b):
    t = b.type
    if t is BoundType.UNBOUNDED_PRECEDING:
        return "UNBOUNDED PRECEDING"
    if t is BoundType.UNBOUNDED_FOLLOWING:
        return "UNBOUNDED FOLLOWING"
    if t is BoundType.CURRENT_ROW:
        return "CURRENT ROW"
    if t is BoundType.PRECEDING:
        return "%d PRECEDING" % b.offset
    return "%d FOLLOWING" % b.offset


def determinism_check():
    print("=" * 78)
    print("可复现性：含大量重复键，重复运行 5 次，比较结果 SHA-256")
    import random
    rng = random.Random(20261006)
    keys = [rng.randint(0, 4) for _ in range(10000)]
    vals = list(range(10000))
    spec = FrameSpec(FrameMode.RANGE, P(2), F(2))
    digests = []
    for _ in range(5):
        r = window_aggregate(spec, keys, vals, "sum")
        digests.append(hashlib.sha256(repr(r).encode()).hexdigest()[:16])
    print("五次运行摘要: %s" % digests)
    print("一致: %s" % (len(set(digests)) == 1))


def main():
    keys = [1, 2, 2, 2, 4]
    vals = [1, 10, 20, 30, 5]

    show("用例1 并列值并入（RANGE，边界不切断并列组）",
         FrameSpec(FrameMode.RANGE, P(1), CUR), keys, vals)
    show("用例2 并列值截断（ROWS，同样的数据按物理行切开并列组）",
         FrameSpec(FrameMode.ROWS, P(1), CUR), keys, vals)
    show("用例3 全帧 UNBOUNDED PRECEDING .. UNBOUNDED FOLLOWING",
         FrameSpec(FrameMode.ROWS, UBP, UBF), keys, vals)
    show("用例4 空帧（ROWS 3 FOLLOWING .. 5 FOLLOWING，仅首行非空）",
         FrameSpec(FrameMode.ROWS, F(3), F(5)), [1, 2, 3, 4], [1, 2, 3, 4])
    show("用例5 RANGE 值空洞导致空帧（1 PRECEDING .. 1 PRECEDING）",
         FrameSpec(FrameMode.RANGE, P(1), P(1)), [1, 10], [100, 200])
    show("用例6 单行分区",
         FrameSpec(FrameMode.RANGE, P(9), F(9)), [42], [7])
    determinism_check()


if __name__ == "__main__":
    main()
