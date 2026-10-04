"""Print dedup merge statistics for a high-concurrency burst.

Run: python3 demo_dedup.py
"""

import asyncio

from retrylib import AsyncDeduper, BackoffPolicy, FakeClock, retry_call
import random


async def main():
    n = 1000
    dedup = AsyncDeduper()
    clock = FakeClock()
    rng = random.Random(42)
    policy = BackoffPolicy(max_attempts=3)
    release = asyncio.Event()
    attempts = 0

    async def downstream():
        nonlocal attempts
        attempts += 1
        await release.wait()
        return b"payload"

    async def op():
        return await retry_call(downstream, policy, rng, clock.sleep)

    tasks = [asyncio.ensure_future(dedup.call("user:123/query", op))
             for _ in range(n)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)

    leaders = [r for r in results if not r.via_dedup]
    print(f"concurrent callers       : {n}")
    print(f"actual executions started: {dedup.stats.started}")
    print(f"merged (dedup hits)      : {dedup.stats.merged}")
    print(f"downstream attempt calls : {attempts}")
    print(f"leaders / joiners        : {len(leaders)} / {n - len(leaders)}")
    print(f"all got same payload     : {all(r.value == b'payload' for r in results)}")


if __name__ == "__main__":
    asyncio.run(main())
