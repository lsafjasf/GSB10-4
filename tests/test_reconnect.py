"""长轮询重连退避自测（python -m unittest discover -s tests -v）。

所有时间均来自 VirtualClock，不触碰真实时钟，可任意快进。
"""

import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from longpoll import (  # noqa: E402
    ClockMovedBackwards,
    IllegalTransition,
    Intent,
    ReconnectPolicy,
    Reconnector,
    RetryAfter,
    SeedJitter,
    State,
    VirtualClock,
)

SEED = 20240601


def make(policy=None, seed=SEED, start=1000.0):
    clock = VirtualClock(start)
    policy = policy or ReconnectPolicy()
    return Reconnector(policy, clock, SeedJitter(seed)), clock


class TestFirstSuccess(unittest.TestCase):
    """首次即成功：start 即 SEND，成功后立刻下一轮，无退避、无抖动消耗。"""

    def test_first_poll_succeeds(self):
        r, clock = make()
        self.assertEqual(r.state, State.IDLE)
        self.assertEqual(r.start(), Intent.SEND)
        self.assertEqual(r.state, State.POLLING)
        self.assertEqual(r.request_started_at, 1000.0)
        self.assertEqual(r.deadline, 1000.0 + r.policy.request_timeout)

        clock.advance(3.0)  # 服务端 3s 后正常返回
        self.assertEqual(r.on_result(), Intent.SEND)  # 立刻下一轮长轮询
        self.assertEqual(r.state, State.POLLING)
        self.assertEqual(r.failures, 0)
        self.assertEqual(r.jitter.calls, 0)  # 成功路径不消耗抖动
        self.assertEqual(r.request_started_at, 1003.0)


class TestConsecutiveTimeouts(unittest.TestCase):
    """连续超时：退避时刻完全确定，与手写期望值逐一核对。"""

    def test_timeout_backoff_schedule(self):
        r, clock = make()
        r.start()
        expected_bases = [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]  # min(2**n, 30)
        rng = random.Random(SEED)
        expected = [rng.random() * b for b in expected_bases]

        t = 1000.0
        for i, want in enumerate(expected, start=1):
            # 推进到超时点（25s 无响应）
            clock.advance(r.policy.request_timeout)
            t += r.policy.request_timeout
            self.assertEqual(r.on_timeout(), Intent.WAIT)
            self.assertEqual(r.state, State.WAITING)
            self.assertEqual(r.failures, i)
            self.assertAlmostEqual(r.reconnect_at - t, want, places=12,
                                   msg="第 %d 次退避时长不符" % i)
            # 拨到重连时刻，on_tick 触发重连
            clock.advance(want)
            t += want
            self.assertEqual(r.on_tick(), Intent.SEND)
            self.assertEqual(r.state, State.POLLING)
            self.assertEqual(r.request_started_at, t)

    def test_tick_before_reconnect_at_keeps_waiting(self):
        r, clock = make()
        r.start()
        clock.advance(25.0)
        r.on_timeout()
        clock.advance(0.001)  # 远未到 reconnect_at
        self.assertEqual(r.on_tick(), Intent.WAIT)
        self.assertEqual(r.state, State.WAITING)

    def test_give_up_at_max_failures(self):
        policy = ReconnectPolicy(max_failures=3)
        r, clock = make(policy)
        r.start()
        intents = []
        for _ in range(3):
            clock.advance(25.0)
            intents.append(r.on_timeout())
            if r.state is State.WAITING:
                clock.advance(r.reconnect_at - clock.now())
                r.on_tick()
        self.assertEqual(intents, [Intent.WAIT, Intent.WAIT, Intent.GIVE_UP])
        self.assertEqual(r.state, State.FAILED)
        self.assertEqual(r.failures, 3)
        # FAILED 后不再重连
        self.assertEqual(r.on_tick(), Intent.GIVE_UP)
        with self.assertRaises(IllegalTransition):
            r.start()

    def test_success_resets_failure_streak(self):
        r, clock = make()
        r.start()
        for _ in range(2):  # 先失败两次
            clock.advance(25.0)
            r.on_timeout()
            clock.advance(r.reconnect_at - clock.now())
            r.on_tick()
        self.assertEqual(r.failures, 2)
        clock.advance(1.0)
        r.on_result()  # 成功
        self.assertEqual(r.failures, 0)
        # 再失败时应从 base_delay 重新开始（第 1 档），而非第 3 档
        clock.advance(25.0)
        r.on_timeout()
        base = r.policy.base_delay  # 1.0，而非 4.0
        self.assertLessEqual(r.reconnect_at - clock.now(), base)


class TestServerRejection(unittest.TestCase):
    """服务端拒绝 + Retry-After：提示优先、上限保护、permanent 直接放弃。"""

    def test_retry_after_takes_priority_over_local_backoff(self):
        r, clock = make()
        r.start()
        clock.advance(2.0)
        intent = r.on_result(retriable=True, retry_after=RetryAfter(7.5))
        self.assertEqual(intent, Intent.WAIT)
        # 采用服务端 7.5s，而非本地 full-jitter 曲线；不消耗抖动
        self.assertAlmostEqual(r.reconnect_at - clock.now(), 7.5)
        self.assertEqual(r.jitter.calls, 0)
        clock.advance(7.5)
        self.assertEqual(r.on_tick(), Intent.SEND)

    def test_retry_after_capped_by_max_server_delay(self):
        r, clock = make()
        r.start()
        clock.advance(1.0)
        r.on_result(retriable=True, retry_after=RetryAfter(99999.0))
        self.assertAlmostEqual(r.reconnect_at - clock.now(),
                               r.policy.max_server_delay)  # 300s 封顶

    def test_retry_after_zero_reconnects_immediately(self):
        r, clock = make()
        r.start()
        clock.advance(1.0)
        r.on_result(retriable=True, retry_after=RetryAfter(0.0))
        self.assertEqual(r.reconnect_at, clock.now())
        self.assertEqual(r.on_tick(), Intent.SEND)

    def test_permanent_rejection_gives_up_immediately(self):
        r, clock = make()
        r.start()
        clock.advance(1.0)
        intent = r.on_result(retriable=True,
                             retry_after=RetryAfter(5.0, permanent=True))
        self.assertEqual(intent, Intent.GIVE_UP)
        self.assertEqual(r.state, State.FAILED)
        self.assertEqual(r.failures, 1)

    def test_rejection_without_hint_falls_back_to_local_curve(self):
        r, clock = make()
        r.start()
        clock.advance(1.0)
        r.on_result(retriable=True, retry_after=None)
        self.assertEqual(r.jitter.calls, 1)  # 用了本地曲线
        self.assertLessEqual(r.reconnect_at - clock.now(), r.policy.base_delay)

    def test_hint_counts_toward_failure_limit(self):
        policy = ReconnectPolicy(max_failures=2)
        r, clock = make(policy)
        r.start()
        clock.advance(1.0)
        r.on_result(retriable=True, retry_after=RetryAfter(5.0))
        clock.advance(5.0)
        r.on_tick()
        clock.advance(1.0)
        # 第二次拒绝即达上界：即使有提示也放弃
        intent = r.on_result(retriable=True, retry_after=RetryAfter(5.0))
        self.assertEqual(intent, Intent.GIVE_UP)
        self.assertEqual(r.state, State.FAILED)


class TestClockRollback(unittest.TestCase):
    """时钟回拨：视为不可恢复故障，进入 FAILED 并抛 ClockMovedBackwards。"""

    def test_rollback_during_waiting(self):
        r, clock = make()
        r.start()
        clock.advance(25.0)
        r.on_timeout()
        self.assertEqual(r.state, State.WAITING)
        clock._t -= 10.0  # 模拟时钟回拨
        with self.assertRaises(ClockMovedBackwards):
            r.on_tick()
        self.assertEqual(r.state, State.FAILED)
        self.assertIn("clock went backwards", r.last_error)

    def test_rollback_during_polling(self):
        r, clock = make()
        r.start()
        clock._t -= 1.0
        with self.assertRaises(ClockMovedBackwards):
            r.on_result()
        self.assertEqual(r.state, State.FAILED)

    def test_reset_recovers_from_failed(self):
        r, clock = make()
        r.start()
        clock._t -= 1.0
        with self.assertRaises(ClockMovedBackwards):
            r.on_result()
        r.reset()
        self.assertEqual(r.state, State.IDLE)
        self.assertEqual(r.failures, 0)
        self.assertEqual(r.start(), Intent.SEND)  # 可重新开始

    def test_micro_skew_within_tolerance_is_ok(self):
        r, clock = make()
        r.start()
        clock._t -= 1e-9  # 容忍范围内的浮点噪声
        self.assertEqual(r.on_result(), Intent.SEND)


class TestJitterReproducibility(unittest.TestCase):
    """同种子序列完全一致；不同种子不同。"""

    def test_same_seed_same_sequence(self):
        a, b = SeedJitter(SEED), SeedJitter(SEED)
        seq_a = [a.sample() for _ in range(100)]
        seq_b = [b.sample() for _ in range(100)]
        self.assertEqual(seq_a, seq_b)
        self.assertEqual(a.calls, 100)

    def test_different_seed_differs(self):
        a, b = SeedJitter(1), SeedJitter(2)
        self.assertNotEqual([a.sample() for _ in range(10)],
                            [b.sample() for _ in range(10)])

    def test_full_schedule_reproducible_across_runs(self):
        def run():
            r, clock = make()
            r.start()
            times = []
            for _ in range(6):
                clock.advance(25.0)
                r.on_timeout()
                times.append(r.reconnect_at)
                clock.advance(r.reconnect_at - clock.now())
                r.on_tick()
            return times

        self.assertEqual(run(), run())


class TestIllegalTransitions(unittest.TestCase):
    def test_premature_timeout_rejected(self):
        r, clock = make()
        r.start()
        clock.advance(10.0)  # 未到 25s 超时
        with self.assertRaises(IllegalTransition):
            r.on_timeout()

    def test_events_in_wrong_state_rejected(self):
        r, _ = make()
        with self.assertRaises(IllegalTransition):
            r.on_result()
        with self.assertRaises(IllegalTransition):
            r.on_timeout()
        r.start()
        with self.assertRaises(IllegalTransition):
            r.start()  # 重复 start

    def test_stop_goes_idle(self):
        r, clock = make()
        r.start()
        clock.advance(25.0)
        r.on_timeout()
        self.assertEqual(r.stop(), Intent.DONE)
        self.assertEqual(r.state, State.IDLE)


class TestBackoffCurve(unittest.TestCase):
    """退避曲线数学性质：封顶、单调（base 部分）。"""

    def test_base_capped_at_max_delay(self):
        policy = ReconnectPolicy()
        caps = []
        for n in range(1, 12):
            base = min(policy.base_delay * policy.multiplier ** (n - 1),
                       policy.max_delay)
            caps.append(base)
        self.assertEqual(caps, [1, 2, 4, 8, 16, 30, 30, 30, 30, 30, 30])

    def test_local_backoff_within_bounds(self):
        policy = ReconnectPolicy()
        j = SeedJitter(7)
        for n in range(1, 20):
            d = policy.local_backoff(n, j)
            base = min(policy.base_delay * policy.multiplier ** (n - 1),
                       policy.max_delay)
            self.assertGreaterEqual(d, 0.0)
            self.assertLessEqual(d, base)
            self.assertTrue(math.isfinite(d))


if __name__ == "__main__":
    unittest.main()
