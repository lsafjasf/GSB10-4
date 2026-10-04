"""仿真：高优先级洪水下低优先级任务的完成等待时间分布。

时间由 FakeClock 注入，结果确定可复现。
运行: python3 simulation.py
"""

import random

from fair_queue import AgingPriorityQueue


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt=1.0):
        self.t += dt


def simulate(lam=0.97, p_low=4, aging=10.0, n_low=2000, seed=20261005):
    rng = random.Random(seed)
    clock = FakeClock()
    q = AgingPriorityQueue(num_priorities=8, aging_interval=aging, clock=clock)
    waits, pending = [], {}
    next_arrival, t, injected = 0.0, 0, 0
    while len(waits) < n_low:
        t += 1
        clock.advance(1)
        while next_arrival <= clock.t:            # 泊松洪水, 速率 lam
            q.enqueue(("flood", next_arrival), priority=0)
            next_arrival += rng.expovariate(lam)
        if t % 50 == 0 and injected < n_low:      # 周期性注入低优先级任务
            tid = ("low", t)
            q.enqueue(tid, priority=p_low)
            pending[tid] = clock.t
            injected += 1
        if len(q) == 0:
            continue
        task = q.dequeue()                        # 服务速率 = 1
        if task.task_id in pending:
            waits.append(clock.t - pending.pop(task.task_id))
    return waits


def report(waits, p_low, aging):
    waits = sorted(waits)
    n = len(waits)
    q = lambda p: waits[min(n - 1, int(n * p))]
    print("scenario: p0 Poisson flood, lambda=0.97, S=1, "
          "low priority p=%d, aging A=%.0f, n=%d" % (p_low, aging, n))
    print("  min=%.0f  p50=%.0f  p90=%.0f  p95=%.0f  p99=%.0f  max=%.0f"
          % (waits[0], q(0.5), q(0.9), q(0.95), q(0.99), waits[-1]))
    print("  theoretical bound: wait <= p*A + B*S = %.0f + B "
          "(B = backlog at t + p*A, bounded when lambda < 1)" % (p_low * aging))


if __name__ == "__main__":
    report(simulate(), p_low=4, aging=10.0)
