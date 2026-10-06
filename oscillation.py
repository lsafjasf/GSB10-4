"""水位震荡抑制演示：同一往复负载分别跑「有冷却」与「无冷却」两个池。

负载：每 1.5s 一个周期，瞬时获取 6 个连接（冲高）再全部释放（归零），
共 12 个周期，周期远短于冷却期 6s。
运行：python3 oscillation.py
"""
from watermark_pool import WatermarkPool


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def build(clock, cooldown, log):
    return WatermarkPool(
        min_size=2, max_size=16, high_water=0.8, low_water=0.3,
        idle_timeout=1.0, up_burst=4, up_rate=4.0,
        down_step=2, down_interval=0.5, cooldown=cooldown,
        time_fn=clock, event_log=log,
    )


def drive(pool, clock, cycles=12):
    per_cycle = []
    for _ in range(cycles):
        got = [pool.acquire(timeout=0) for _ in range(6)]
        for h in got:
            h.release()
        clock.advance(1.5)
        pool.reap()
        st = pool.stats()
        per_cycle.append((clock.t, st["size"], st["busy"]))
    return per_cycle


def count(log):
    seq = [e[1] for e in log if e[1] in ("grow", "close")]
    return (
        seq.count("grow"),
        seq.count("close"),
        sum(1 for a, b in zip(seq, seq[1:]) if a != b),
    )


def main():
    clock_a, log_a = FakeClock(), []
    clock_b, log_b = FakeClock(), []
    pool_a = build(clock_a, 6.0, log_a)
    pool_b = build(clock_b, 0.0, log_b)
    cyc_a = drive(pool_a, clock_a)
    cyc_b = drive(pool_b, clock_b)

    print("== 池A：cooldown=6.0 + 滞回带(0.3,0.8) 的扩缩状态序列 ==")
    print(f"{'t(s)':>6} {'event':>8} {'cid':>4} {'size':>5} {'busy':>5}  why")
    for t, kind, cid, size, busy, why in log_a:
        if kind in ("grow", "close"):
            print(f"{t:6.1f} {kind:>8} {cid:>4} {size:>5} {busy:>5}  {why}")

    print("\n== 各周期末状态（A 有冷却 / B 无冷却） ==")
    print(f"{'t(s)':>6} {'A.size':>7} {'A.busy':>7} {'B.size':>7} {'B.busy':>7}")
    for (ta, sa, ba), (tb, sb, bb) in zip(cyc_a, cyc_b):
        print(f"{ta:6.1f} {sa:>7} {ba:>7} {sb:>7} {bb:>7}")

    ga, ca, fa = count(log_a)
    gb, cb, fb = count(log_b)
    print("\n== 震荡抑制结果（12 周期） ==")
    print(f"{'':>12} {'grow':>6} {'close':>6} {'dir_flip':>9} {'total':>7}")
    print(f"{'A cooldown':>12} {ga:>6} {ca:>6} {fa:>9} {ga + ca:>7}")
    print(f"{'B no-cooldown':>12} {gb:>6} {cb:>6} {fb:>9} {gb + cb:>7}")


if __name__ == "__main__":
    main()
