"""对拍：DUT（WatermarkPool）vs 无界理想池参考模型。

参考模型语义：无容量上限、无缩容，每个请求到达立即授予唯一连接，释放必须一一对应。
对拍期间每一步都校验：
- DUT 授予的连接绝不同时属于两个在途请求（cid -> 请求唯一）；
- DUT 在途计数之和 == DUT 已授予未释放的请求数；
- 库内关闭连接时 in_flight 必须为 0（库内 assert 兜底）；
- 终态：两模型 acquired/released/completed 完全相等，dropped == 0，规模回落到 min。
"""
import random
import sys

from watermark_pool import WatermarkPool


class Oracle:
    """无界理想池：到达即授予，释放严格配对。"""

    def __init__(self):
        self.next_cid = 0
        self.active = {}        # req -> cid
        self.acquired = 0
        self.released = 0

    def grant(self, req):
        assert req not in self.active
        self.active[req] = self.next_cid
        self.next_cid += 1
        self.acquired += 1

    def release(self, req):
        assert req in self.active
        del self.active[req]
        self.released += 1


def run_seed(seed, verbose=False):
    rng = random.Random(seed)
    clock = [0.0]
    min_size = rng.randint(1, 4)
    max_size = rng.randint(min_size, min_size + rng.randint(4, 24))
    pool = WatermarkPool(
        min_size=min_size, max_size=max_size,
        low_water=rng.choice([0.2, 0.3, 0.4]),
        high_water=rng.choice([0.6, 0.7, 0.8]),
        expand_step=rng.choice([1, 2, 4]),
        shrink_step=rng.choice([1, 2, 4]),
        expand_rate=rng.choice([2, 4, 8]),
        expand_window=1.0,
        idle_timeout=rng.choice([1.0, 2.0, 5.0]),
        cooldown=rng.choice([0.0, 0.5, 1.0]),
        time_fn=lambda: clock[0])
    oracle = Oracle()

    n_reqs = rng.randint(50, 400)
    # 请求到达时间表
    arrivals = []
    t = 0.0
    for req in range(n_reqs):
        t += rng.expovariate(4.0)
        hold = rng.expovariate(1.0)
        if rng.random() < 0.05:          # 5% 长尾请求
            hold = rng.uniform(8.0, 30.0)
        arrivals.append((t, req, hold))
    arrivals.reverse()

    waiting = []                          # DUT 尚未授予的请求
    dut_active = {}                       # req -> (cid, release_at)
    cid2req = {}                          # cid -> req：唯一性对拍
    max_queue = 0
    steps = 0
    while (arrivals or waiting or dut_active) and steps < 100000:
        steps += 1
        clock[0] += rng.uniform(0.05, 0.4)

        # 1) 到达：oracle 立即授予，DUT 入等待队列
        while arrivals and arrivals[-1][0] <= clock[0]:
            at, req, hold = arrivals.pop()
            oracle.grant(req)
            waiting.append((req, hold))

        # 2) DUT 按 FIFO 尝试授予
        head = 0
        still = []
        while head < len(waiting):
            req, hold = waiting[head]
            cid = pool.try_acquire()
            if cid is None:
                break
            assert cid not in cid2req, "同一连接同时授予两个请求"
            cid2req[cid] = req
            dut_active[req] = (cid, clock[0] + hold)
            head += 1
        waiting = waiting[head:]
        max_queue = max(max_queue, len(waiting))

        # 3) 到期释放：oracle 必须处于 active，DUT 必须持有该 cid
        due = [r for r, (_, at) in dut_active.items() if at <= clock[0]]
        for req in due:
            cid, _ = dut_active.pop(req)
            assert cid2req.pop(cid) == req
            pool.release(cid)
            oracle.release(req)

        # 4) 小概率强制摘除（健康检查下线），在途也必须安全结清
        if rng.random() < 0.02 and cid2req:
            cid = rng.choice(list(cid2req))
            pool.drain(cid)

        pool.tick(clock[0])
        pool.check_invariants()

        # 5) 账目对拍：DUT 在途数 == 库内 in_flight 之和
        total_in_flight = sum(c.in_flight for c in pool._conns.values())
        assert total_in_flight == len(dut_active) == len(cid2req), \
            (total_in_flight, len(dut_active), len(cid2req))

    # 收尾：时间推进足够久，规模必须回落到 min，且无请求丢失
    for _ in range(200):
        clock[0] += 10.0
        pool.tick(clock[0])
        pool.check_invariants()
        if pool.snapshot()["size"] == min_size:
            break
    assert not waiting and not dut_active and not arrivals, "有请求未结清"
    assert oracle.acquired == n_reqs == pool.stats["acquired"]
    assert oracle.released == n_reqs == pool.stats["released"]
    assert pool.stats["dropped"] == 0
    assert pool.snapshot()["size"] == min_size
    return {
        "seed": seed, "n": n_reqs, "min": min_size, "max": max_size,
        "acquired": pool.stats["acquired"], "released": pool.stats["released"],
        "expanded": pool.stats["expanded"], "shrunk": pool.stats["shrunk"],
        "drained": pool.stats["drained"], "max_queue": max_queue,
        "final": pool.snapshot()["size"],
    }


def main():
    n_seeds = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    print("对拍 DUT(WatermarkPool) vs Oracle(无界理想池), seeds=%d" % n_seeds)
    print("%-5s %-4s %-4s %-4s %-9s %-9s %-9s %-7s %-7s %-9s %-6s %s"
          % ("seed", "n", "min", "max", "acquired", "released", "expanded",
             "shrunk", "drained", "max_queue", "final", "result"))
    totals = dict(acquired=0, released=0, expanded=0, shrunk=0, drained=0)
    shown = set([0, 1, 2, 7, 42, 99, n_seeds - 1])
    for seed in range(n_seeds):
        r = run_seed(seed)
        for k in totals:
            totals[k] += r[k]
        if seed in shown:
            print("%-5d %-4d %-4d %-4d %-9d %-9d %-9d %-7d %-7d %-9d %-6d PASS"
                  % (r["seed"], r["n"], r["min"], r["max"], r["acquired"],
                     r["released"], r["expanded"], r["shrunk"], r["drained"],
                     r["max_queue"], r["final"]))
    print("-" * 92)
    print("合计: seeds=%d acquired=%d released=%d expanded=%d shrunk=%d drained=%d"
          % (n_seeds, totals["acquired"], totals["released"], totals["expanded"],
             totals["shrunk"], totals["drained"]))
    assert totals["acquired"] == totals["released"]
    print("对拍结论: 全部请求一一配对结清，dropped=0，无连接在途被关闭，规模最终回落 min。")


if __name__ == "__main__":
    main()
