"""100 万次获取/释放压测：调度耗时 + 水位变化曲线。

用注入的仿真时钟（每轮 1ms）驱动水位逻辑，用 perf_counter 单独计量
真实调度耗时；负载分 低载->高载->回落 三个相位，并混入 0.1% 长尾请求。
扩容被限速时调用方收到背压（PoolExhausted），下一轮重试，不丢请求。

运行：python3 bench.py [ops]     # 默认 1_000_000
输出：控制台耗时汇总 + curve.csv（水位变化曲线）
"""
import heapq
import sys
import time

from watermark_pool import PoolExhausted, WatermarkPool


def hold_ops(i, ops):
    """第 i 个请求的持有时长（单位：op 轮次）。"""
    frac = i / ops
    if frac < 0.25:
        base = 20          # 低载
    elif frac < 0.75:
        base = 150         # 高载
    elif frac < 0.97:
        base = 20          # 回落
    else:
        base = 1           # 尾部空载，观察水位归位
    if i % 997 == 0 and frac < 0.90:
        base = 30_000      # 长尾：约 0.1%，持有 30 仿真秒
    return base


def main(ops=1_000_000):
    sim = [0.0]
    pool = WatermarkPool(
        min_size=4, max_size=256, high_water=0.8, low_water=0.3,
        idle_timeout=5.0, up_burst=8, up_rate=32.0,
        down_step=4, down_interval=0.5, cooldown=3.0,
        time_fn=lambda: sim[0],
    )
    pending = []  # (due_op, handle) 最小堆：模拟请求的持有与归还
    curve = []
    acquired = released = backpressure = 0
    max_size = 0

    t0 = time.perf_counter()
    while acquired < ops:
        sim[0] += 0.001  # 每轮 1ms 仿真时间
        try:
            h = pool.acquire(timeout=0)
        except PoolExhausted:
            backpressure += 1
        else:
            heapq.heappush(pending,
                           (acquired + hold_ops(acquired, ops), acquired, h))
            acquired += 1
        while pending and pending[0][0] <= acquired:
            _, _, done = heapq.heappop(pending)
            done.release()
            released += 1
        if acquired % 5000 == 0 and (not curve or curve[-1][0] != acquired):
            st = pool.stats()
            max_size = max(max_size, st["size"])
            curve.append((acquired, round(sim[0], 1), st["size"],
                          st["busy"], st["in_flight"]))
    while pending:  # 收尾结清
        _, _, done = heapq.heappop(pending)
        done.release()
        released += 1
    dt = time.perf_counter() - t0

    with open("curve.csv", "w") as f:
        f.write("op,sim_t_s,size,busy,in_flight\n")
        for row in curve:
            f.write(",".join(map(str, row)) + "\n")

    st = pool.stats()
    print("== 100 万次获取/释放 调度耗时 ==")
    print(f"获取 acquire         : {acquired}")
    print(f"释放 release         : {released}")
    print(f"背压重试 backpressure: {backpressure}")
    print(f"总耗时               : {dt:.3f} s")
    print(f"吞吐                 : {acquired / dt:,.0f} acquire/s "
          f"({(acquired + released) / dt:,.0f} op/s)")
    print(f"单次获取均耗         : {dt / acquired * 1e6:.2f} us")
    print(f"结束时在途           : {st['in_flight']}（应归零）")
    print(f"size 峰值 / 终值     : {max_size} / {st['size']}")

    print("\n== 水位变化曲线（抽样，完整见 curve.csv） ==")
    print(f"{'op':>9} {'sim_t(s)':>9} {'size':>5} {'busy':>5} {'in_flight':>9}")
    for row in curve[:: max(1, len(curve) // 24)]:
        print(f"{row[0]:>9} {row[1]:>9} {row[2]:>5} {row[3]:>5} {row[4]:>9}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000)
