"""自测：虚拟时钟 + 固定种子，所有重连时刻必须完全确定。"""

import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from longpoll.reconnect import (  # noqa: E402
    BackoffPolicy,
    FailReason,
    Reconnector,
    State,
    VirtualClock,
)

# 固定策略：base=1s, factor=2, cap=60s, 超时=30s, 上界=12
POLICY = BackoffPolicy(request_timeout=30.0, base_delay=1.0, factor=2.0,
                       max_delay=60.0, max_attempts=12)
SEED = 20261005

# 种子 SEED 下 rng.random() 的前 12 个值（用于核对可复现性）
EXPECTED_RAW_RANDOM = [
    0.2768271010047326, 0.29632958322643, 0.15449398081647991,
    0.5534013176105713, 0.021929920401406844, 0.1117443570255302,
    0.2871892715678781, 0.2844437341182724, 0.8836087095895984,
    0.640805888274815, 0.5866771169470939, 0.856304350344132,
]


def make_rng():
    return random.Random(SEED)


def make(clock=None, policy=None):
    return Reconnector(clock or VirtualClock(),
                       policy or POLICY, make_rng().random)


class TestFirstSuccess(unittest.TestCase):
    def test_success_on_first_attempt_resets_everything(self):
        clk = VirtualClock()
        rc = make(clk)
        self.assertEqual(rc.state, State.IDLE)
        rc.start_poll()
        self.assertEqual(rc.state, State.CONNECTING)
        clk.advance(10)  # 30s 超时前返回
        self.assertFalse(rc.has_timed_out())
        rc.on_success()
        self.assertEqual(rc.state, State.IDLE)
        self.assertEqual(rc.failures, 0)
        self.assertIsNone(rc.next_due)


class TestConsecutiveTimeouts(unittest.TestCase):
    def test_rng_sequence_is_reproducible(self):
        rng = make_rng()
        self.assertEqual([rng.random() for _ in range(12)],
                         EXPECTED_RAW_RANDOM)

    def test_backoff_curve_and_reconnect_instants(self):
        clk = VirtualClock()
        rc = make(clk)
        # 退避曲线：delay(n) = min(2**(n-1), 60)/2 * (1 + u)
        raw = [POLICY.capped_delay(n) for n in range(1, 12)]
        self.assertEqual(raw, [1, 2, 4, 8, 16, 32, 60, 60, 60, 60, 60])

        instants = []  # 每次“可以重连”的单调时刻
        rc.start_poll()
        t = 0.0
        for n in range(1, 6):  # 连续 5 次超时
            t += POLICY.request_timeout
            clk.advance(POLICY.request_timeout)
            self.assertTrue(rc.has_timed_out())
            delay = rc.on_failure(FailReason.TIMEOUT)
            expected = raw[n - 1] / 2.0 * (1.0 + EXPECTED_RAW_RANDOM[n - 1])
            self.assertAlmostEqual(delay, expected, places=12)
            t += delay
            # 窗口未结束不得重连
            self.assertFalse(rc.wait_elapsed())
            clk.advance(delay)
            self.assertTrue(rc.wait_elapsed())
            instants.append(clk.now())
            rc.start_poll()

        # 给定虚拟时钟后每次重连时刻完全确定（无抖动种子时的基准见 data/backoff_sequence.json）
        expected_instants = []
        acc = 0.0
        for n in range(5):
            acc += POLICY.request_timeout
            acc += raw[n] / 2.0 * (1.0 + EXPECTED_RAW_RANDOM[n])
            expected_instants.append(acc)
        for got, want in zip(instants, expected_instants):
            self.assertAlmostEqual(got, want, places=9)

    def test_identical_seed_identical_instants(self):
        def run():
            clk = VirtualClock()
            rc = make(clk)
            out = []
            for _ in range(8):
                rc.start_poll()
                clk.advance(POLICY.request_timeout)
                d = rc.on_failure(FailReason.TIMEOUT)
                clk.advance(d)
                out.append((rc.failures, round(clk.now(), 12), round(d, 12)))
            return out
        self.assertEqual(run(), run())


class TestServerRejection(unittest.TestCase):
    def test_retry_after_takes_precedence_over_local_backoff(self):
        clk = VirtualClock()
        rc = make(clk)
        rc.start_poll()
        # 本地退避本应是约 0.97s，但服务端明确要求 25s
        local = POLICY.jittered_delay(1, make_rng().random)
        self.assertLess(local, 1.0)
        clk.advance(1)
        delay = rc.on_failure(FailReason.REJECTED, server_hint=25.0)
        self.assertEqual(delay, 25.0)
        self.assertEqual(rc.failures, 1)  # 提示不改失败事实
        self.assertEqual(rc.next_due, clk.now() + 25.0)

    def test_retry_after_is_clamped_to_max_delay(self):
        clk = VirtualClock()
        rc = make(clk)
        rc.start_poll()
        delay = rc.on_failure(FailReason.REJECTED, server_hint=9999.0)
        self.assertEqual(delay, POLICY.max_delay)

    def test_negative_hint_falls_back_to_local_backoff(self):
        clk = VirtualClock()
        rc = make(clk)
        rc.start_poll()
        delay = rc.on_failure(FailReason.REJECTED, server_hint=-1)
        local = POLICY.jittered_delay(1, make_rng().random)
        self.assertAlmostEqual(delay, local, places=12)

    def test_hint_still_counts_toward_give_up(self):
        clk = VirtualClock()
        rc = make(clk)
        for _ in range(POLICY.max_attempts - 1):
            rc.start_poll()
            rc.on_failure(FailReason.REJECTED, server_hint=0.0)
        self.assertEqual(rc.state, State.WAITING)
        rc.start_poll()
        d = rc.on_failure(FailReason.REJECTED, server_hint=5.0)
        self.assertTrue(math.isinf(d))
        self.assertEqual(rc.state, State.GIVEN_UP)


class TestMaxAttemptsAndRecovery(unittest.TestCase):
    def test_give_up_after_bound_and_reset(self):
        clk = VirtualClock()
        rc = make(clk)
        for i in range(POLICY.max_attempts):
            rc.start_poll()
            clk.advance(POLICY.request_timeout)
            rc.on_failure(FailReason.TIMEOUT)
            if rc.state is State.WAITING:
                clk.advance(rc.last_delay)
        self.assertEqual(rc.state, State.GIVEN_UP)
        self.assertEqual(rc.failures, POLICY.max_attempts)
        with self.assertRaises(RuntimeError):
            rc.start_poll()
        rc.reset()
        self.assertEqual(rc.state, State.IDLE)
        self.assertEqual(rc.failures, 0)
        rc.start_poll()
        clk.advance(5)
        rc.on_success()  # 恢复后首次即成功

    def test_success_anywhere_resets_streak(self):
        clk = VirtualClock()
        rc = make(clk)
        for _ in range(POLICY.max_attempts - 2):
            rc.start_poll()
            clk.advance(POLICY.request_timeout)
            rc.on_failure(FailReason.TIMEOUT)
            clk.advance(rc.last_delay)
        rc.start_poll()
        clk.advance(1)
        rc.on_success()
        self.assertEqual(rc.failures, 0)
        # 重新失败时从 n=1 的退避重新开始
        rc.start_poll()
        clk.advance(POLICY.request_timeout)
        delay = rc.on_failure(FailReason.TIMEOUT)
        consumed = POLICY.max_attempts - 2  # 之前失败已消耗的随机数个数
        expected = POLICY.capped_delay(1) / 2.0 * (1.0 + EXPECTED_RAW_RANDOM[consumed])
        self.assertAlmostEqual(delay, expected, places=12)


class TestClockRollback(unittest.TestCase):
    def test_rollback_does_not_fire_timeout_early(self):
        clk = VirtualClock(1000.0)
        rc = make(clk)
        rc.start_poll()  # deadline = mono 1030
        clk.advance(20)
        self.assertFalse(rc.has_timed_out())
        clk.rollback(500)  # 墙上时间回到 520
        self.assertFalse(rc.has_timed_out())
        clk.advance(9)   # 单调视图 1029，仍不足 30s
        self.assertFalse(rc.has_timed_out())
        clk.advance(1)
        self.assertTrue(rc.has_timed_out())

    def test_rollback_during_wait_does_not_trigger_reconnect(self):
        clk = VirtualClock()
        rc = make(clk)
        rc.start_poll()
        clk.advance(POLICY.request_timeout)
        delay = rc.on_failure(FailReason.TIMEOUT, server_hint=None)
        self.assertGreater(delay, 0)
        due_before = rc.next_due
        clk.advance(delay / 2)
        self.assertFalse(rc.wait_elapsed())  # 读时钟，提交已流逝的半程
        clk.rollback(10 ** 6)         # 大幅回拨
        self.assertFalse(rc.wait_elapsed())
        clk.advance(delay / 2 - 1e-9)  # 单调增量补上剩余窗口
        self.assertFalse(rc.wait_elapsed())
        clk.advance(1e-9 + 1e-6)
        self.assertTrue(rc.wait_elapsed())
        self.assertEqual(rc.next_due, due_before)

    def test_rollback_cannot_reset_failure_streak(self):
        clk = VirtualClock()
        rc = make(clk)
        rc.start_poll()
        clk.advance(POLICY.request_timeout)
        rc.on_failure(FailReason.TIMEOUT)
        clk.rollback(10 ** 9)
        self.assertEqual(rc.failures, 1)
        self.assertEqual(rc.state, State.WAITING)


class TestEventLoopDriver(unittest.TestCase):
    def test_poll_state_transitions(self):
        clk = VirtualClock()
        rc = make(clk)
        event, _ = rc.poll()
        self.assertEqual(event, "connecting")
        clk.advance(POLICY.request_timeout)
        event, delay = rc.poll()
        self.assertEqual(event, "timeout")
        self.assertAlmostEqual(
            delay, POLICY.capped_delay(1) / 2.0 * (1 + EXPECTED_RAW_RANDOM[0]))
        event, remaining = rc.poll()
        self.assertEqual(event, "waiting")
        self.assertAlmostEqual(remaining, delay, places=9)
        clk.advance(delay)
        event, _ = rc.poll()
        self.assertEqual(event, "reconnecting")


class TestSequenceDataFile(unittest.TestCase):
    """data/backoff_sequence.json 必须与库的实际调度逐位一致。"""

    def test_json_schedule_matches_library(self):
        import json
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "backoff_sequence.json")
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["seed"], SEED)
        clk = VirtualClock()
        rc = make(clk)
        for row in doc["schedule"]:
            rc.start_poll()
            clk.advance(POLICY.request_timeout)
            self.assertAlmostEqual(clk.now(), row["failure_at"], places=9)
            delay = rc.on_failure(FailReason.TIMEOUT)
            if row["state"] == "GIVEN_UP":
                self.assertEqual(rc.state, State.GIVEN_UP)
                self.assertTrue(math.isinf(delay))
            else:
                self.assertAlmostEqual(delay, row["applied_delay"], places=12)
                clk.advance(delay)
                self.assertAlmostEqual(clk.now(), row["reconnect_at"], places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
