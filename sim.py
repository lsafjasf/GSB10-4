"""100 万次获取/释放调度仿真：给出调度耗时与水位变化曲线。

虚拟时钟 + 确定性随机（seed=42），负载分五阶段：
  A 空闲低载  : 利用率长期低于低水位，观察缩容回落 min
  B 爬坡扩容  : 到达率线性爬升，观察限速扩容（令牌桶 + 单步上界）
  C 过载触顶  : 需求超过 max_size，观察触顶与排队（无丢弃）
  D 长尾请求  : 2% 请求持用 20~60s，观察在途保护
  E 负载回落  : 请求结清后空闲超时，观察安全缩容
运行：python3 sim.py [请求总数] [seed]
产物：watermark_curve.csv
"""
import csv
import heapq
import random
import sys
import time as realtime
from collections import deque

from watermark_pool import WatermarkPool

TOTAL = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 42
DT = 0.5
MIN_SIZE, MAX_SIZE = 16, 4096


def arrival_rate(t):
    if t < 100.0:
        return 5.0                       # A 空闲低载
    if t < 280.0:
        return 5.0 + (2500.0 - 5.0) * (t - 100.0) / 180.0  # B 爬坡
    if t < 420.0:
        return 4500.0                   # C 过载（需求 4500*1.5=6750 > 4096）
    if t < 600.0:
        return 1200.0                   # D 长尾
    return 20.0                          # E 回落


def phase_of(t):
    return "ABCDE"[min(4, int(t // 1000) if False else
                       (0 if t < 100 else 1 if t < 280 else
                        2 if t < 420 else 3 if t < 600 else 4))]


def sparkline(values, width=80):
    blocks = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    bucket = len(values) / width
    out = []
    for i in range(width):
        seg = values[int(i * bucket):max(1, int((i + 1) * bucket))]
        if not seg:
            seg = [values[-1]]
        v = sum(seg) / len(seg)
        out.append(blocks[min(7, int((v - lo) / span * 7.999))])
    return "".join(out)


def main():
    rng = random.Random(SEED)
    clock = [0.0]
    pool = WatermarkPool(
        min_size=MIN_SIZE, max_size=MAX_SIZE,
        low_water=0.30, high_water=0.70,
        expand_step=64, shrink_step=32,
        expand_rate=512, expand_window=1.0,
        idle_timeout=20.0, cooldown=3.0,
        time_fn=lambda: clock[0])

    arrivals_left = TOTAL
    queue = deque()          # 等待授予的请求：持用时长
    releases = []            # (释放时刻, cid)
    samples = []
    phase_info = {}
    max_queue = 0
    granted_ever = 0

    t0 = realtime.perf_counter()
    while releases or queue or arrivals_left:
        clock[0] += DT
        t = clock[0]

        # 1) 到期释放（在途请求结清）
        while releases and releases[0][0] <= t:
            _, cid = heapq.heappop(releases)
            pool.release(cid)

        # 2) FIFO 服务排队请求
        while queue:
            cid = pool.try_acquire()
            if cid is None:
                break
            hold = queue.popleft()
            heapq.heappush(releases, (t + hold, cid))
            granted_ever += 1

        # 3) 新到达
        if arrivals_left:
            lam = arrival_rate(t)
            mean = lam * DT
            n = int(mean) + (1 if rng.random() < mean - int(mean) else 0)
            n = min(n, arrivals_left)
            arrivals_left -= n
            in_d = 420.0 <= t < 600.0
            for _ in range(n):
                if in_d and rng.random() < 0.02:
                    queue.append(rng.uniform(20.0, 60.0))   # 长尾
                else:
                    queue.append(rng.uniform(0.5, 2.5))

        # 4) 水位调度
        pool.tick(t)

        # 5) 排队请求若有空闲连接会在下一轮拿到；记录曲线
        snap = pool.snapshot()
        samples.append((t, snap["size"], snap["busy"], len(queue)))
        max_queue = max(max_queue, len(queue))
        phase_info.setdefault(phase_of(t), t)

    wall = realtime.perf_counter() - t0
    assert not queue and not releases
    assert pool.stats["released"] == TOTAL
    assert pool.stats["dropped"] == 0

    # 回落缩容到 min（仿真主循环结束时若长尾刚结清，再跑若干 tick）
    for _ in range(400):
        clock[0] += 5.0
        pool.tick(clock[0])
        samples.append((clock[0], pool.snapshot()["size"],
                        pool.snapshot()["busy"], 0))
        if pool.snapshot()["size"] == MIN_SIZE:
            break

    with open("watermark_curve.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["virtual_time_s", "pool_size", "busy", "queued"])
        w.writerows(samples)

    sizes = [s[1] for s in samples]
    phase_ranges = [("A 空闲低载", 0, 100), ("B 爬坡扩容", 100, 280),
                    ("C 过载触顶", 280, 420), ("D 长尾在途", 420, 600),
                    ("E 回落缩容", 600, None)]
    print("=" * 78)
    print("100 万次获取/释放调度仿真 (seed=%d, 总获取=%d, 总释放=%d)" %
          (SEED, TOTAL, pool.stats["released"]))
    print("-" * 78)
    for name, a, b in phase_ranges:
        seg = [s for s in samples if s[0] >= a and (b is None or s[0] < b)]
        if not seg:
            continue
        print("阶段 %-8s 规模 %4d -> %-4d  峰值 %4d  峰值忙 %4d  峰值排队 %6d"
              % (name, seg[0][1], seg[-1][1], max(x[1] for x in seg),
                 max(x[2] for x in seg), max(x[3] for x in seg)))
    print("-" * 78)
    print("扩容事件=%d (共 +%d 连接)  缩容事件=%d (共 -%d 连接)  "
          "冷却抑制=%d  忙连接被跳过=%d"
          % (pool.stats["scale_up_events"], pool.stats["expanded"],
             pool.stats["scale_down_events"], pool.stats["shrunk"],
             pool.stats["suppressed_by_cooldown"],
             pool.stats["suppressed_shrink_busy"]))
    print("最终规模=%d (min=%d, max=%d)  全程最大排队=%d  dropped=%d"
          % (pool.snapshot()["size"], MIN_SIZE, MAX_SIZE,
             max_queue, pool.stats["dropped"]))
    ops = TOTAL * 2
    print("调度耗时: %.3f s   吞吐: %.0f ops/s (获取+释放共 %d 次)" %
          (wall, ops / wall, ops))
    print("-" * 78)
    print("规模水位曲线 (16 ~ 4096, 左=虚拟时间 0s, 右=%.0fs):" % samples[-1][0])
    print(sparkline(sizes))
    print("=" * 78)
    print("明细曲线已写入 watermark_curve.csv")


if __name__ == "__main__":
    main()
