"""AgingPriorityQueue 自测：时间全部由 FakeClock 注入，结果确定可复现。

运行: python3 -m unittest test_fair_queue -v
"""

import unittest

from fair_queue import AgingPriorityQueue, Empty


class FakeClock:
    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, dt=1.0):
        self.t += dt


def make_queue(num_priorities=8, aging=10.0):
    clock = FakeClock()
    return AgingPriorityQueue(num_priorities=num_priorities,
                              aging_interval=aging, clock=clock), clock


def drain(queue, clock, service_time=0.0):
    """抽空队列，返回 [(completion_time, task_id), ...]。"""
    out = []
    while len(queue):
        out.append((clock.t, queue.dequeue().task_id))
        clock.advance(service_time)
    return out


class TestBasicOrdering(unittest.TestCase):
    def test_single_priority_fifo(self):
        q, c = make_queue()
        for i in range(5):
            q.enqueue(i, priority=3)
            c.advance()
        self.assertEqual([t for _, t in drain(q, c)], [0, 1, 2, 3, 4])

    def test_higher_priority_first(self):
        q, c = make_queue()
        q.enqueue("low", priority=5)
        q.enqueue("high", priority=0)
        q.enqueue("mid", priority=2)
        self.assertEqual([t for _, t in drain(q, c)], ["high", "mid", "low"])

    def test_all_same_priority_fifo_with_gaps(self):
        # 全部同优先级、不同入队时刻：严格按入队顺序完成
        q, c = make_queue()
        for i in range(10):
            q.enqueue(i, priority=4)
            c.advance(3)
        self.assertEqual([t for _, t in drain(q, c)], list(range(10)))

    def test_same_instant_fifo_tiebreak(self):
        # 同一时刻入队（键完全相同）：按入队序号 FIFO
        q, c = make_queue()
        for i in range(100):
            q.enqueue(i, priority=2)
        self.assertEqual([t for _, t in drain(q, c)], list(range(100)))


class TestAging(unittest.TestCase):
    def test_aging_overtakes_later_high_priority(self):
        # 低优先级任务等待 p*A 后，键小于之后到达的高优先级任务
        q, c = make_queue(aging=10.0)
        q.enqueue("low", priority=3)          # K = 30
        c.advance(31)
        q.enqueue("high", priority=0)         # K = 31 > 30
        self.assertEqual([t for _, t in drain(q, c)], ["low", "high"])

    def test_no_aging_before_interval(self):
        q, c = make_queue(aging=10.0)
        q.enqueue("low", priority=3)          # K = 30
        c.advance(29)
        q.enqueue("high", priority=0)         # K = 29 < 30
        self.assertEqual([t for _, t in drain(q, c)], ["high", "low"])

    def test_starvation_bound_under_infinite_flood(self):
        # 无限高优先级洪水：每个时间单位到达 1 个 p=0 任务、服务 1 个任务。
        # 低优先级任务 L (p=2, A=10) 的键 K=20，理论完成时刻上界 = t + p*A + B*S。
        q, c = make_queue(aging=10.0)
        q.enqueue("L", priority=2)            # K = 20
        completed = {}
        t = 0
        for t in range(1, 10_000):
            c.advance(1)
            q.enqueue(("flood", t), priority=0)   # 洪水永不停止
            task = q.dequeue()
            completed[task.task_id] = c.t
            if "L" in completed:
                break
        # L 恰在 t=20 完成：t=20 入队的洪水任务键=20，与 L 相同但序号更大
        self.assertEqual(completed["L"], 20.0)
        self.assertLessEqual(completed["L"], 0 + 2 * 10.0)  # t + p*A 上界

    def test_two_level_flood_bound(self):
        # 两级洪水（p=0 与 p=1 同时灌入），低优先级 p=3 仍有界
        q, c = make_queue(aging=10.0)
        q.enqueue("L", priority=3)            # K = 30
        done = None
        for t in range(1, 10_000):
            c.advance(1)
            q.enqueue(("f0", t), priority=0)  # 键 = t
            q.enqueue(("f1", t), priority=1)  # 键 = 10 + t
            task = q.dequeue()                # 服务速率 1 < 到达速率 2（过饱和）
            if task.task_id == "L":
                done = c.t
                break
        # 只有键 < 30 的任务能排在 L 前：p=0 的 t<30 与 p=1 的 t<20，
        # 全部在有限窗口内到达，L 必被调度
        self.assertIsNotNone(done)
        # 键 < 30 的任务共 29 个 p0 (t=1..29) + 19 个 p1 (t=1..19)，
        # 每步服务 1 个，第 49 次出队轮到 L
        self.assertEqual(done, 49.0)
        self.assertLessEqual(done, 0 + 3 * 10.0 + 19)  # t + p*A + B*S 上界

    def test_completion_order_same_priority_under_flood(self):
        # 同级任务在洪水下仍保持 FIFO 完成顺序
        q, c = make_queue(aging=10.0)
        lows = [("L", i) for i in range(5)]
        for i, tid in enumerate(lows):
            q.enqueue(tid, priority=4)
            c.advance(2)                      # 键 = 40, 42, 44, 46, 48
        order = []
        for t in range(1, 10_000):
            c.advance(1)
            q.enqueue(("flood", t), priority=0)
            task = q.dequeue()
            if isinstance(task.task_id, tuple) and task.task_id[0] == "L":
                order.append(task.task_id)
            if len(order) == 5:
                break
        self.assertEqual(order, lows)         # 严格 FIFO


class TestCancelAndTimeout(unittest.TestCase):
    def test_cancel_preserves_relative_order(self):
        q, c = make_queue()
        for i in range(6):
            q.enqueue(i, priority=2)
        self.assertTrue(q.cancel(2))
        self.assertTrue(q.cancel(4))
        self.assertEqual([t for _, t in drain(q, c)], [0, 1, 3, 5])
        self.assertEqual(len(q), 0)

    def test_cancel_unknown_and_double(self):
        q, c = make_queue()
        q.enqueue("a", priority=1)
        self.assertFalse(q.cancel("nope"))
        self.assertTrue(q.cancel("a"))
        self.assertFalse(q.cancel("a"))       # 重复取消
        self.assertEqual(len(q), 0)
        with self.assertRaises(Empty):
            q.dequeue()

    def test_cancel_dequeued_task_fails(self):
        q, c = make_queue()
        q.enqueue("a")
        q.dequeue()
        self.assertFalse(q.cancel("a"))

    def test_timeout_skipped_order_preserved(self):
        q, c = make_queue()
        q.enqueue("short", priority=0, timeout=5)
        q.enqueue("a", priority=1)
        q.enqueue("b", priority=1)
        c.advance(6)                          # "short" 已超时
        self.assertEqual([t for _, t in drain(q, c)], ["a", "b"])
        self.assertEqual(len(q), 0)

    def test_timeout_zero_expires_immediately(self):
        q, c = make_queue()
        q.enqueue("x", timeout=0)
        q.enqueue("y", priority=7)
        self.assertEqual(q.dequeue().task_id, "y")
        with self.assertRaises(Empty):
            q.dequeue()

    def test_continuous_cancel_flood(self):
        # 持续取消洪流：入队 5000 个任务，随机取消一半，交错出队，
        # 幸存者必须严格按 (键, 序号) 顺序出现，队列结构不被破坏
        import random
        rng = random.Random(42)
        q, c = make_queue()
        expected = []                          # (key, seq, task_id)
        cancelled = set()
        seq = 0
        for i in range(5000):
            prio = rng.randrange(8)
            q.enqueue(i, priority=prio)
            expected.append((prio * 10.0 + c.t, seq, i))
            seq += 1
            c.advance(rng.random())
            if rng.random() < 0.5:
                victim = rng.randrange(i + 1)
                if victim not in cancelled and q.cancel(victim):
                    cancelled.add(victim)
            if i % 7 == 0 and len(q):
                task = q.dequeue()             # 交错出队
                expected = [e for e in expected if e[2] != task.task_id]
        survivors = sorted(e for e in expected if e[2] not in cancelled)
        got = [t for _, t in drain(q, c)]
        self.assertEqual(got, [e[2] for e in survivors])
        self.assertEqual(len(q), 0)


class TestEdgeCases(unittest.TestCase):
    def test_empty_dequeue(self):
        q, _ = make_queue()
        with self.assertRaises(Empty):
            q.dequeue()

    def test_duplicate_task_id(self):
        q, _ = make_queue()
        q.enqueue("a")
        with self.assertRaises(ValueError):
            q.enqueue("a")
        q.dequeue()
        q.enqueue("a")                         # 出队后 id 可复用

    def test_invalid_priority(self):
        q, _ = make_queue(num_priorities=4)
        for bad in (-1, 4, 100):
            with self.assertRaises(ValueError):
                q.enqueue("x", priority=bad)

    def test_invalid_constructor_args(self):
        with self.assertRaises(ValueError):
            AgingPriorityQueue(num_priorities=0)
        with self.assertRaises(ValueError):
            AgingPriorityQueue(aging_interval=0)

    def test_negative_timeout(self):
        q, _ = make_queue()
        with self.assertRaises(ValueError):
            q.enqueue("x", timeout=-1)

    def test_len_and_contains(self):
        q, _ = make_queue()
        q.enqueue("a", priority=1)
        q.enqueue("b", priority=1)
        self.assertEqual(len(q), 2)
        self.assertIn("a", q)
        q.cancel("a")
        self.assertNotIn("a", q)
        self.assertEqual(len(q), 1)


class TestCompletionDistribution(unittest.TestCase):
    """高优先级洪水下低优先级任务的完成等待时间分布（确定性场景）。"""

    def test_distribution_within_bound(self):
        # 高优先级泊松洪水（lambda=0.97，服务速率=1，保持持续积压），
        # 每 100 个时间单位注入 1 个 p=4 低优先级任务，共 200 个样本。
        import random
        rng = random.Random(20261005)
        aging = 10.0
        p_low = 4
        q, c = make_queue(aging=aging)
        waits = []
        pending = {}
        next_arrival = 0.0
        max_backlog = 0
        for t in range(1, 20_201):
            c.advance(1)
            while next_arrival <= c.t:       # lambda=0.97 泊松到达
                q.enqueue(("flood", next_arrival), priority=0)
                next_arrival += rng.expovariate(0.97)
            if t % 100 == 37 and t <= 20_000:
                tid = ("low", t)
                q.enqueue(tid, priority=p_low)
                pending[tid] = c.t
            max_backlog = max(max_backlog, len(q))
            if len(q) == 0:
                continue
            task = q.dequeue()
            if task.task_id in pending:
                waits.append(c.t - pending.pop(task.task_id))
        self.assertEqual(len(waits), 200)
        waits.sort()
        n = len(waits)
        stats = {
            "min": waits[0],
            "p50": waits[n // 2],
            "p95": waits[int(n * 0.95)],
            "p99": waits[int(n * 0.99)],
            "max": waits[-1],
        }
        # 理论上界 completion <= t + p*A + B*S；lambda < 1 时 B（积压）有界
        hard_bound = p_low * aging + max_backlog
        for name, value in stats.items():
            self.assertLessEqual(value, hard_bound, name)
        print("\n[distribution] low-priority wait under Poisson p0 flood "
              "(p=4, A=10, S=1, lambda=0.97, n=200): " +
              " ".join("%s=%.1f" % kv for kv in stats.items()) +
              " | max backlog B=%d, bound=%.1f" % (max_backlog, hard_bound))


if __name__ == "__main__":
    unittest.main(verbosity=2)
