"""Self-tests for retrylib. Run: python3 -m unittest -v"""

import asyncio
import random
import unittest

from retrylib import (
    AsyncDeduper,
    BackoffPolicy,
    FakeClock,
    JitterStrategy,
    RetryResult,
    Status,
    retry_call,
)


class StubRng:
    """Scripted rng: uniform() returns absolute values in order."""

    def __init__(self, values):
        self._values = list(values)
        self.calls = []

    def uniform(self, a, b):
        self.calls.append((a, b))
        return self._values.pop(0)


# ---------------------------------------------------------------- jitter

class TestJitter(unittest.TestCase):
    def test_full_jitter_bounds_randomized(self):
        rng = random.Random(7)
        policy = BackoffPolicy(base=0.1, cap=1.0, strategy=JitterStrategy.FULL)
        for attempt in range(8):
            c = policy.ceiling(attempt)
            for _ in range(1000):
                d = policy.delay(attempt, rng)
                self.assertGreaterEqual(d, 0.0)
                self.assertLessEqual(d, c)

    def test_equal_jitter_bounds_randomized(self):
        rng = random.Random(7)
        policy = BackoffPolicy(base=0.1, cap=1.0, strategy=JitterStrategy.EQUAL)
        for attempt in range(8):
            c = policy.ceiling(attempt)
            for _ in range(1000):
                d = policy.delay(attempt, rng)
                self.assertGreaterEqual(d, c / 2.0)
                self.assertLessEqual(d, c)

    def test_full_jitter_boundary_values(self):
        # attempt=2, base=0.5 -> ceiling = 0.5 * 4 = 2.0
        policy = BackoffPolicy(base=0.5, cap=100.0, strategy=JitterStrategy.FULL)
        stub = StubRng([0.0, 2.0])
        self.assertEqual(policy.delay(2, stub), 0.0)   # lower bound hit
        self.assertEqual(policy.delay(2, stub), 2.0)   # upper bound hit
        self.assertEqual(stub.calls, [(0.0, 2.0), (0.0, 2.0)])

    def test_equal_jitter_boundary_values(self):
        # ceiling = 2.0 -> delay = 1.0 + U(0, 1.0), bounds [1.0, 2.0]
        policy = BackoffPolicy(base=0.5, cap=100.0, strategy=JitterStrategy.EQUAL)
        stub = StubRng([0.0, 1.0])
        self.assertEqual(policy.delay(2, stub), 1.0)
        self.assertEqual(policy.delay(2, stub), 2.0)
        self.assertEqual(stub.calls, [(0.0, 1.0), (0.0, 1.0)])

    def test_ceiling_is_capped(self):
        policy = BackoffPolicy(base=0.1, cap=10.0)
        self.assertEqual(policy.ceiling(0), 0.1)
        self.assertEqual(policy.ceiling(100), 10.0)

    def test_none_strategy_is_deterministic(self):
        policy = BackoffPolicy(base=0.1, cap=10.0, strategy=JitterStrategy.NONE)
        rng = random.Random(0)
        self.assertEqual(policy.delay(3, rng), policy.ceiling(3))

    def test_reproducible_sequence_fixed_seed(self):
        policy = BackoffPolicy()  # base=0.1, cap=10, FULL

        def seq():
            rng = random.Random(42)
            return [policy.delay(i, rng) for i in range(10)]

        a, b = seq(), seq()
        self.assertEqual(a, b)
        # Pinned golden sequence (seed=42) - guards against silent changes.
        expected = [
            0.06394267984578837,
            0.005002151044533387,
            0.1100117273476477,
            0.1785685905190582,
            1.1783539426624199,
            2.1654383597533164,
            5.709949233311011,
            0.8693883262941615,
            4.2192181968527045,
            0.29797219438070344,
        ]
        self.assertEqual(a, expected)


# ------------------------------------------------------- retry, no concurrency

class TestRetryNoConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_success_after_two_failures(self):
        clock = FakeClock()
        rng = random.Random(42)
        policy = BackoffPolicy(base=0.1, cap=10.0, max_attempts=5)
        calls = 0

        async def fn():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("flaky")
            return "ok"

        res = await retry_call(fn, policy, rng, clock.sleep)
        self.assertIs(res.status, Status.SUCCEEDED)
        self.assertTrue(res.ok)
        self.assertFalse(res.via_dedup)
        self.assertEqual(res.value, "ok")
        self.assertEqual(res.attempts, 3)
        self.assertEqual(len(res.delays), 2)
        for i, d in enumerate(res.delays):
            self.assertGreaterEqual(d, 0.0)
            self.assertLessEqual(d, policy.ceiling(i))
        self.assertAlmostEqual(clock.now, sum(res.delays))

    async def test_exhausted_is_distinct_result(self):
        clock = FakeClock()
        rng = random.Random(1)
        policy = BackoffPolicy(max_attempts=4)
        boom = RuntimeError("boom")

        async def fn():
            raise boom

        res = await retry_call(fn, policy, rng, clock.sleep)
        self.assertIs(res.status, Status.EXHAUSTED)
        self.assertFalse(res.ok)
        self.assertFalse(res.via_dedup)
        self.assertIs(res.error, boom)
        self.assertEqual(res.attempts, 4)
        self.assertEqual(len(res.delays), 3)
        # EXHAUSTED and a dedup hit are different shapes entirely:
        self.assertNotEqual(res.status, Status.SUCCEEDED)
        self.assertFalse(res.dedup_hit)

    async def test_no_real_time_passes(self):
        # Injected clock: 5 retries with cap=10s finish instantly in wall time.
        import time

        clock = FakeClock()
        rng = random.Random(9)
        policy = BackoffPolicy(max_attempts=6)

        async def fn():
            raise RuntimeError("x")

        t0 = time.perf_counter()
        await retry_call(fn, policy, rng, clock.sleep)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertGreater(clock.now, 0.0)  # virtual time did advance


# ------------------------------------------------------------------- dedup

class TestDedup(unittest.IsolatedAsyncioTestCase):
    async def test_high_concurrency_merges_to_one_execution(self):
        n = 200
        clock = FakeClock()
        rng = random.Random(3)
        policy = BackoffPolicy(max_attempts=3)
        dedup = AsyncDeduper()
        release = asyncio.Event()
        exec_count = 0

        async def fn():
            nonlocal exec_count
            exec_count += 1
            await release.wait()
            raise RuntimeError("downstream down")

        async def op():
            return await retry_call(fn, policy, rng, clock.sleep)

        tasks = [asyncio.ensure_future(dedup.call("same-key", op)) for _ in range(n)]
        for _ in range(3):
            await asyncio.sleep(0)  # let every caller register/join
        release.set()
        results = await asyncio.gather(*tasks)

        leaders = [r for r in results if not r.via_dedup]
        joiners = [r for r in results if r.via_dedup]
        self.assertEqual(len(leaders), 1)
        self.assertEqual(len(joiners), n - 1)
        self.assertEqual(exec_count, 3)  # only the leader ran: 3 attempts
        self.assertEqual(dedup.stats.started, 1)
        self.assertEqual(dedup.stats.joined, n - 1)
        self.assertEqual(dedup.stats.merged, n - 1)
        self.assertEqual(dedup.stats.total_calls, n)
        # Exhaustion and dedup-hit stay distinguishable for every caller:
        for r in results:
            self.assertIs(r.status, Status.EXHAUSTED)
        self.assertFalse(leaders[0].via_dedup)
        self.assertTrue(all(r.dedup_hit for r in joiners))

    async def test_dedup_shares_success_value(self):
        dedup = AsyncDeduper()
        release = asyncio.Event()

        async def op():
            await release.wait()
            return RetryResult(status=Status.SUCCEEDED, value={"row": 1}, attempts=1)

        t1 = asyncio.ensure_future(dedup.call("k", op))
        t2 = asyncio.ensure_future(dedup.call("k", op))
        await asyncio.sleep(0)
        release.set()
        r1, r2 = await asyncio.gather(t1, t2)
        self.assertEqual(r1.value, r2.value)
        self.assertEqual(sorted([r1.via_dedup, r2.via_dedup]), [False, True])
        self.assertEqual(dedup.stats.merged, 1)

    async def test_distinct_keys_not_merged(self):
        dedup = AsyncDeduper()
        release = asyncio.Event()
        exec_count = 0

        async def op():
            nonlocal exec_count
            exec_count += 1
            await release.wait()
            return RetryResult(status=Status.SUCCEEDED, attempts=1)

        tasks = [
            asyncio.ensure_future(dedup.call(f"key-{i % 2}", op))
            for i in range(10)
        ]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(exec_count, 2)  # one per distinct key
        self.assertEqual(dedup.stats.started, 2)
        self.assertEqual(dedup.stats.joined, 8)

    async def test_sequential_same_key_not_merged(self):
        dedup = AsyncDeduper()

        async def op():
            return RetryResult(status=Status.SUCCEEDED, value=1, attempts=1)

        await dedup.call("k", op)
        await dedup.call("k", op)
        self.assertEqual(dedup.stats.started, 2)
        self.assertEqual(dedup.stats.joined, 0)


if __name__ == "__main__":
    unittest.main()
