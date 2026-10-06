"""对拍：WatermarkPool vs 参照模型（Oracle），同一随机操作序列双跑比对。

Oracle 用最直白的方式记录每个请求的生命周期（签发/存活/结清），
被测池每走一步都与 Oracle 比对在途数，并检查全部水位不变量；
最后验证：所有签发的请求全部安全结清，缩容过程零丢失。

运行：python3 duipai.py [ops] [seed]
"""
import random
import sys

from watermark_pool import PoolExhausted, WatermarkPool


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class Oracle:
    """参照模型：只认请求签发与结清，任何丢失/重复都会触发断言。"""

    def __init__(self):
        self.live = set()
        self.issued = 0
        self.completed = 0

    def issue(self):
        self.issued += 1
        rid = self.issued
        assert rid not in self.live
        self.live.add(rid)
        return rid

    def complete(self, rid):
        assert rid in self.live, f"请求 {rid} 重复结清或未签发"
        self.live.discard(rid)
        self.completed += 1


def main(ops=200_000, seed=277):
    rng = random.Random(seed)
    clock = FakeClock()
    log = []
    pool = WatermarkPool(
        min_size=4, max_size=64, high_water=0.8, low_water=0.3,
        idle_timeout=2.0, up_burst=8, up_rate=16.0,
        down_step=2, down_interval=0.5, cooldown=1.0,
        time_fn=clock, event_log=log,
    )
    oracle = Oracle()
    live = {}  # rid -> handle
    backpressure = 0
    mismatches = 0
    min_size_seen, max_size_seen = pool.stats()["size"], 0
    samples = []

    for i in range(ops):
        clock.advance(rng.random() * 0.02)
        # 约 1% 概率触发一次外部清理（模拟定时 reaper）
        if rng.random() < 0.01:
            pool.reap()
        if live and rng.random() < 0.5:
            rid = rng.choice(list(live))
            live.pop(rid).release()
            oracle.complete(rid)
        else:
            try:
                h = pool.acquire(timeout=0)
            except PoolExhausted:
                backpressure += 1  # 背压：请求稍后再试，不算丢失
                continue
            rid = oracle.issue()
            live[rid] = h

        st = pool.stats()
        min_size_seen = min(min_size_seen, st["size"])
        max_size_seen = max(max_size_seen, st["size"])
        # 逐步对拍：在途请求数必须与 Oracle 完全一致
        if st["in_flight"] != len(oracle.live):
            mismatches += 1
        assert 4 <= st["size"] <= 64, "水位越界"
        assert st["in_flight"] >= 0
        if i % (ops // 10) == 0:
            samples.append((i, clock.t, st["size"], st["busy"],
                            st["in_flight"], len(oracle.live)))

    # 收尾：结清全部在途请求
    for rid, h in live.items():
        h.release()
        oracle.complete(rid)
    live.clear()

    final = pool.stats()
    closed = [e for e in log if e[1] == "close"]
    grown = [e for e in log if e[1] == "grow"]

    print("== 对拍采样（pool vs oracle 逐步比对） ==")
    print(f"{'op':>8} {'t(s)':>8} {'size':>5} {'busy':>5} "
          f"{'pool_inflight':>13} {'oracle_live':>11}")
    for row in samples:
        print(f"{row[0]:>8} {row[1]:>8.1f} {row[2]:>5} {row[3]:>5} "
              f"{row[4]:>13} {row[5]:>11}")

    print("\n== 对拍结果 ==")
    print(f"操作总数 ops            : {ops}")
    print(f"签发请求 issued         : {oracle.issued}")
    print(f"安全结清 completed      : {oracle.completed}")
    print(f"背压重试 backpressure   : {backpressure}")
    print(f"在途不一致次数 mismatch : {mismatches}")
    print(f"扩容事件 grow           : {len(grown)}")
    print(f"缩容事件 close          : {len(closed)}")
    print(f"size 观测区间           : [{min_size_seen}, {max_size_seen}]")
    print(f"结束时在途 in_flight    : {final['in_flight']}")

    assert mismatches == 0, "对拍发现不一致"
    assert oracle.completed == oracle.issued, "有请求未结清（丢失）"
    assert final["in_flight"] == 0
    assert min_size_seen >= 4 and max_size_seen <= 64
    print("\n对拍通过：请求零丢失，水位不变量全程成立。")


if __name__ == "__main__":
    ops = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 277
    main(ops, seed)
