"""limiter 自测：覆盖正常 / 异常 / 超时 / 取消 / 重复归还 / 高并发守恒。

运行：python3 test_limiter.py -v
"""

import asyncio
import threading
import unittest

from limiter import (
    AcquireTimeoutError,
    AsyncConcurrencyLimiter,
    ConcurrencyLimiter,
    DoubleReleaseError,
    InvalidTokenError,
)


def show(tag, audit):
    print(
        f"    [{tag}] available={audit['available']} held={audit['held']} "
        f"issued={audit['issued_total']} returned={audit['returned_total']} "
        f"conserved={audit['conserved']}"
    )


class TestSyncPaths(unittest.TestCase):
    def test_normal_path(self):
        print("\n== 正常路径 ==")
        lim = ConcurrencyLimiter(3)
        t1 = lim.acquire()
        t2 = lim.acquire()
        show("获取2个后", lim.audit())
        self.assertEqual(lim.audit()["available"], 1)
        lim.release(t1)
        show("归还1个后", lim.audit())
        lim.release(t2)
        a = lim.audit()
        show("全部归还后", a)
        self.assertEqual(a["available"], 3)
        self.assertEqual(a["issued_total"], a["returned_total"])
        self.assertTrue(a["conserved"])

    def test_exception_path(self):
        print("\n== 异常路径（with 块内抛错，finally 必须归还）==")
        lim = ConcurrencyLimiter(2)
        show("初始", lim.audit())
        with self.assertRaises(RuntimeError):
            with lim.hold():
                show("持令牌中", lim.audit())
                raise RuntimeError("boom")
        a = lim.audit()
        show("异常退出后", a)
        self.assertEqual(a["available"], 2)
        self.assertTrue(a["conserved"])

    def test_timeout_path(self):
        print("\n== 超时路径（acquire 超时返回 None，不产生令牌）==")
        lim = ConcurrencyLimiter(1)
        token = lim.acquire()
        show("占满后", lim.audit())
        result = lim.acquire(timeout=0.05)
        a = lim.audit()
        show("超时后", a)
        self.assertIsNone(result)
        self.assertEqual(a["issued_total"], 1)  # 超时不发令牌
        self.assertTrue(a["conserved"])
        lim.release(token)
        show("归还后", lim.audit())

    def test_hold_timeout_raises(self):
        lim = ConcurrencyLimiter(1)
        token = lim.acquire()
        with self.assertRaises(AcquireTimeoutError):
            with lim.hold(timeout=0.02):
                pass
        a = lim.audit()
        self.assertEqual(a["held"], 1)
        self.assertTrue(a["conserved"])
        lim.release(token)

    def test_double_release_detected(self):
        print("\n== 重复归还检出 ==")
        lim = ConcurrencyLimiter(1)
        token = lim.acquire()
        seq = lim.release(token)
        show("正常归还后", lim.audit())
        with self.assertRaises(DoubleReleaseError) as ctx:
            lim.release(token)
        err = ctx.exception
        print(f"    检出: {err}")
        a = lim.audit()
        show("重复归还拒绝后", a)
        self.assertEqual(err.token_id, token.token_id)
        self.assertEqual(err.first_return_seq, seq)
        self.assertEqual(a["double_release_attempts"], 1)
        self.assertEqual(a["available"], 1)  # 计数未被污染
        self.assertEqual(a["returned_total"], 1)
        self.assertTrue(a["conserved"])

    def test_foreign_token_rejected(self):
        print("\n== 串用令牌检出 ==")
        lim_a, lim_b = ConcurrencyLimiter(1), ConcurrencyLimiter(1)
        foreign = lim_b.acquire()
        with self.assertRaises(InvalidTokenError):
            lim_a.release(foreign)
        a = lim_a.audit()
        print(f"    检出: invalid_release_attempts={a['invalid_release_attempts']}")
        self.assertEqual(a["invalid_release_attempts"], 1)
        self.assertTrue(a["conserved"])
        lim_b.release(foreign)

    def test_conservation_accounting(self):
        print("\n== 守恒核算：issued == returned + held ==")
        lim = ConcurrencyLimiter(4)
        tokens = [lim.acquire() for _ in range(3)]
        for t in tokens[:2]:
            lim.release(t)
        a = lim.audit()
        show("发放3归还2持有1", a)
        self.assertEqual(a["issued_total"], 3)
        self.assertEqual(a["returned_total"], 2)
        self.assertEqual(a["held"], 1)
        self.assertEqual(a["issued_total"], a["returned_total"] + a["held"])
        lim.release(tokens[2])

    def test_concurrent_stress(self):
        print("\n== 高并发压测：8线程 x 20000次获取归还 ==")
        lim = ConcurrencyLimiter(4)
        threads_n, iters = 8, 20_000
        errors = []

        def worker():
            try:
                for _ in range(iters):
                    token = lim.acquire(timeout=5.0)
                    assert token is not None
                    lim.release(token)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        a = lim.audit()
        show("压测结束", a)
        self.assertEqual(a["issued_total"], threads_n * iters)
        self.assertEqual(a["returned_total"], threads_n * iters)
        self.assertEqual(a["held"], 0)
        self.assertEqual(a["available"], 4)
        self.assertTrue(a["conserved"])


class TestAsyncPaths(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_while_holding(self):
        print("\n== 取消路径（持令牌时被取消，finally 归还）==")
        lim = AsyncConcurrencyLimiter(1)

        async def job():
            async with lim.hold():
                show("持令牌中", lim.audit())
                await asyncio.sleep(60)

        task = asyncio.create_task(job())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        a = lim.audit()
        show("取消后", a)
        self.assertEqual(a["available"], 1)
        self.assertEqual(a["held"], 0)
        self.assertTrue(a["conserved"])

    async def test_cancel_while_waiting(self):
        print("\n== 取消路径（排队等待时被取消，不产生令牌）==")
        lim = AsyncConcurrencyLimiter(1)
        held = await lim.acquire()

        waiter = asyncio.create_task(lim.acquire())
        await asyncio.sleep(0.05)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        a = lim.audit()
        show("等待取消后", a)
        self.assertEqual(a["issued_total"], 1)  # 未多发令牌
        self.assertTrue(a["conserved"])
        lim.release(held)

    async def test_async_timeout(self):
        print("\n== 异步超时路径 ==")
        lim = AsyncConcurrencyLimiter(1)
        held = await lim.acquire()
        result = await lim.acquire(timeout=0.05)
        a = lim.audit()
        show("超时后", a)
        self.assertIsNone(result)
        self.assertEqual(a["issued_total"], 1)
        self.assertTrue(a["conserved"])
        lim.release(held)

    async def test_async_double_release(self):
        print("\n== 异步重复归还检出 ==")
        lim = AsyncConcurrencyLimiter(1)
        token = await lim.acquire()
        lim.release(token)
        with self.assertRaises(DoubleReleaseError) as ctx:
            lim.release(token)
        print(f"    检出: {ctx.exception}")
        a = lim.audit()
        self.assertEqual(a["double_release_attempts"], 1)
        self.assertTrue(a["conserved"])

    async def test_async_concurrent_stress(self):
        print("\n== 异步并发压测：200任务 x 1000次 ==")
        lim = AsyncConcurrencyLimiter(8)

        async def worker():
            for _ in range(1000):
                async with lim.hold():
                    await asyncio.sleep(0)

        await asyncio.gather(*(worker() for _ in range(200)))
        a = lim.audit()
        show("压测结束", a)
        self.assertEqual(a["issued_total"], 200_000)
        self.assertEqual(a["returned_total"], 200_000)
        self.assertEqual(a["held"], 0)
        self.assertEqual(a["available"], 8)
        self.assertTrue(a["conserved"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
