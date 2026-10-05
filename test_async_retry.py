"""Self-tests. Stdlib only: python3 test_async_retry.py -v"""
import asyncio
import random
import unittest

from async_retry import (
    RetryExecutor, RetryExhausted, Outcome, full_jitter_delay,
    SOURCE_EXECUTED, SOURCE_DEDUPLICATED,
)

ULP_ONE = 2 ** -53  # largest float strictly below 1.0 (M bit 52 => 2^-53)


class FakeClock:
    """Deterministic time: sleep() records the delay instead of waiting."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.t += delay

    def now(self):
        return self.t


class JitterMathTests(unittest.TestCase):
    def test_bounds_and_capping(self):
        # uncapped attempt: limit = base * 2**attempt
        self.assertEqual(
            full_jitter_delay(2, base=0.1, cap=5.0, rand=lambda: 0.0), 0.0)
        # largest rand < 1 -> delay strictly below, arbitrarily close to, limit
        self.assertAlmostEqual(
            full_jitter_delay(2, base=0.1, cap=5.0, rand=lambda: 1.0 - ULP_ONE),
            0.4 * (1.0 - ULP_ONE), places=15)
        # attempt=6 would give 6.4 > cap 5.0 -> capped at 5.0
        self.assertAlmostEqual(
            full_jitter_delay(6, base=0.1, cap=5.0, rand=lambda: 1.0 - ULP_ONE),
            5.0 * (1.0 - ULP_ONE), places=15)

    def test_seed_reproducible_sequence(self):
        def seq():
            rnd = random.Random(20261006)
            return [full_jitter_delay(a, base=0.1, cap=5.0, rand=rnd.random)
                    for a in range(8)]

        self.assertEqual(seq(), seq())  # same seed => byte-identical sequence


class RetryFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_concurrency_success_after_retries(self):
        clock = FakeClock()
        rnd = random.Random(42)
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError(f"boom {calls}")
            return "ok"

        ex = RetryExecutor(max_attempts=5, base_delay=0.1, cap_delay=5.0,
                           sleep=clock.sleep, rand=rnd.random, now=clock.now)
        outcome = await ex.execute("k1", flaky)

        self.assertEqual(outcome, Outcome("ok", 3, SOURCE_EXECUTED))
        self.assertEqual(calls, 3)
        self.assertEqual(len(clock.sleeps), 2)
        # jitter delays are reproducible for seed 42 (attempt 0: limit 0.1)
        self.assertEqual(clock.sleeps[0],
                         full_jitter_delay(0, base=0.1, cap=5.0,
                                           rand=random.Random(42).random))
        self.assertTrue(all(0.0 <= d < 0.4 for d in clock.sleeps))
        self.assertEqual(ex.stats.merged, 0)

    async def test_jitter_boundary_values(self):
        # boundary: zero delay (rand = 0)
        clock0 = FakeClock()
        ex0 = RetryExecutor(max_attempts=3, sleep=clock0.sleep,
                            rand=lambda: 0.0)
        calls = 0

        async def fail_twice():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ValueError()
            return "z"

        out = await ex0.execute("b", fail_twice)
        self.assertEqual(clock0.sleeps, [0.0, 0.0])
        self.assertEqual(out.value, "z")

        # boundary: upper bound — cap is never exceeded
        clock1 = FakeClock()
        ex1 = RetryExecutor(max_attempts=3, base_delay=10.0, cap_delay=0.5,
                            sleep=clock1.sleep, rand=lambda: 1.0 - ULP_ONE)
        try:
            await ex1.execute("b2", always_fail)
        except RetryExhausted:
            pass
        self.assertTrue(all(0.0 <= d < 0.5 for d in clock1.sleeps))
        self.assertAlmostEqual(max(clock1.sleeps), 0.5 * (1.0 - ULP_ONE))

    async def test_retry_exhausted_distinct_from_outcome(self):
        clock = FakeClock()
        ex = RetryExecutor(max_attempts=4, sleep=clock.sleep,
                           rand=lambda: 0.0)
        with self.assertRaises(RetryExhausted) as ctx:
            await ex.execute("dead", always_fail)

        err = ctx.exception
        self.assertEqual(err.attempts, 4)
        self.assertIsInstance(err.last_error, RuntimeError)
        self.assertEqual(err.key, "dead")
        self.assertEqual(len(clock.sleeps), 3)  # no sleep after final attempt
        # it is an exception, never an Outcome
        self.assertNotIsInstance(err, Outcome)

    async def test_high_concurrency_dedup_counted(self):
        clock = FakeClock()
        ex = RetryExecutor(max_attempts=3, base_delay=0.1, cap_delay=1.0,
                           sleep=clock.sleep, seed=7)
        gate = asyncio.Event()
        calls = 0

        async def one_real_request():
            nonlocal calls
            calls += 1
            await gate.wait()
            return "single-result"

        n = 1000
        callers = [asyncio.ensure_future(ex.execute("hot-key", one_real_request))
                   for _ in range(n)]
        await asyncio.sleep(0)          # let the real task reach gate.wait()
        self.assertEqual(ex._inflight.__len__(), 1)
        gate.set()
        results = await asyncio.gather(*callers)

        self.assertEqual(calls, 1)                      # fn ran once
        self.assertEqual(ex.stats.submitted, n)
        self.assertEqual(ex.stats.executed, 1)
        self.assertEqual(ex.stats.merged, n - 1)       # 999 merged
        self.assertEqual([r.source for r in results].count(SOURCE_EXECUTED), 1)
        self.assertEqual(
            [r.source for r in results].count(SOURCE_DEDUPLICATED), n - 1)
        self.assertTrue(all(r.value == "single-result" for r in results))
        self.assertTrue(all(r.attempts == 1 for r in results))
        self.assertNotIn("hot-key", ex._inflight)      # cleaned up after

    async def test_consecutive_batches_run_separately(self):
        # after the in-flight task finishes, a later equal key executes again
        clock = FakeClock()
        ex = RetryExecutor(max_attempts=2, sleep=clock.sleep,
                           rand=lambda: 0.0)
        self.assertEqual((await ex.execute("k", return_ok("a"))).source,
                         SOURCE_EXECUTED)
        self.assertEqual((await ex.execute("k", return_ok("b"))).source,
                         SOURCE_EXECUTED)
        self.assertEqual(ex.stats.executed, 2)
        self.assertEqual(ex.stats.merged, 0)

    async def test_dedup_propagates_exhaustion_distinctly(self):
        # a shared task that exhausts retries: every waiter sees the
        # RetryExhausted exception, not a dedup Outcome
        clock = FakeClock()
        ex = RetryExecutor(max_attempts=2, sleep=clock.sleep,
                           rand=lambda: 0.0)
        gate = asyncio.Event()

        async def failing_shared():
            await gate.wait()
            raise RuntimeError("nope")

        callers = [asyncio.ensure_future(ex.execute("x", failing_shared))
                   for _ in range(50)]
        await asyncio.sleep(0)
        gate.set()
        errors = await asyncio.gather(*callers, return_exceptions=True)
        self.assertEqual(sum(isinstance(e, RetryExhausted) for e in errors), 50)
        self.assertEqual(ex.stats.merged, 49)
        self.assertEqual(ex.stats.executed, 1)


async def always_fail():
    raise RuntimeError("always")


def return_ok(v):
    async def _f():
        return v
    return _f


if __name__ == "__main__":
    unittest.main(verbosity=2)
