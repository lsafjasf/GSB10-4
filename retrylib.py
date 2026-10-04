"""Async retry with jittered backoff and in-flight request deduplication.

Standard library only. Time is injected through a ``sleep`` callable, so
tests and benchmarks run against a deterministic virtual clock instead of
the real wall clock.

Result semantics (important: these are distinct):
  * Status.SUCCEEDED          - call succeeded on some attempt.
  * Status.EXHAUSTED          - all attempts failed; retries used up.
  * RetryResult.via_dedup     - this caller did not execute the request
                                itself; it joined another in-flight call
                                (dedup hit). Orthogonal to Status, so
                                "exhausted" and "dedup hit" can never be
                                confused.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, replace
from enum import Enum
from typing import Awaitable, Callable, Dict, Generic, Hashable, List, Optional, Tuple, TypeVar

T = TypeVar("T")


class JitterStrategy(Enum):
    NONE = "none"
    FULL = "full"      # sleep = U(0, ceiling)
    EQUAL = "equal"    # sleep = ceiling/2 + U(0, ceiling/2)


@dataclass(frozen=True)
class BackoffPolicy:
    base: float = 0.1                # seconds, ceiling for attempt 0
    cap: float = 10.0                # hard upper bound on one delay (seconds)
    max_attempts: int = 5
    strategy: JitterStrategy = JitterStrategy.FULL

    def ceiling(self, attempt: int) -> float:
        """Exponential ceiling *before* jitter (0-based attempt index)."""
        if attempt < 0:
            raise ValueError("attempt must be >= 0")
        return min(self.cap, self.base * (2.0 ** attempt))

    def delay(self, attempt: int, rng: random.Random) -> float:
        """Jittered delay for a 0-based failed attempt.

        FULL : U(0, c),  mean c/2,  bounds [0, c]
        EQUAL: c/2 + U(0, c/2), mean 3c/4, bounds [c/2, c]
        NONE : c (no randomness)
        """
        c = self.ceiling(attempt)
        if self.strategy is JitterStrategy.NONE:
            return c
        if self.strategy is JitterStrategy.FULL:
            return rng.uniform(0.0, c)
        if self.strategy is JitterStrategy.EQUAL:
            return c / 2.0 + rng.uniform(0.0, c / 2.0)
        raise AssertionError(f"unknown strategy: {self.strategy}")


class Status(Enum):
    SUCCEEDED = "succeeded"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class RetryResult(Generic[T]):
    status: Status
    value: Optional[T] = None
    error: Optional[BaseException] = None
    attempts: int = 0
    delays: Tuple[float, ...] = ()
    via_dedup: bool = False  # True => joined a concurrent identical request

    @property
    def ok(self) -> bool:
        return self.status is Status.SUCCEEDED

    @property
    def dedup_hit(self) -> bool:
        return self.via_dedup


Sleep = Callable[[float], Awaitable[None]]
Factory = Callable[[], Awaitable[T]]


async def retry_call(
    fn: Factory,
    policy: BackoffPolicy,
    rng: random.Random,
    sleep: Sleep,
) -> RetryResult:
    """Run ``fn`` with retries. Never raises: failures become EXHAUSTED."""
    delays: List[float] = []
    for attempt in range(policy.max_attempts):
        try:
            value = await fn()
        except Exception as exc:  # noqa: BLE001 - converted into a result
            if attempt == policy.max_attempts - 1:
                return RetryResult(
                    status=Status.EXHAUSTED,
                    error=exc,
                    attempts=attempt + 1,
                    delays=tuple(delays),
                )
            d = policy.delay(attempt, rng)
            delays.append(d)
            await sleep(d)
        else:
            return RetryResult(
                status=Status.SUCCEEDED,
                value=value,
                attempts=attempt + 1,
                delays=tuple(delays),
            )
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass
class DedupStats:
    started: int = 0   # unique in-flight executions started
    joined: int = 0    # callers merged into an existing in-flight call

    @property
    def total_calls(self) -> int:
        return self.started + self.joined

    @property
    def merged(self) -> int:
        """Number of duplicate requests that were merged away."""
        return self.joined


class AsyncDeduper:
    """Merges concurrent calls carrying the same request key.

    The first call for a key runs the factory; concurrent calls for the
    same key await the same task and receive a copy of the result marked
    ``via_dedup=True``. Serial calls with the same key are not merged.
    """

    def __init__(self) -> None:
        self._inflight: Dict[Hashable, "asyncio.Task[RetryResult]"] = {}
        self.stats = DedupStats()

    async def call(self, key: Hashable, factory: Factory) -> RetryResult:
        existing = self._inflight.get(key)
        if existing is not None:
            self.stats.joined += 1
            shared = await asyncio.shield(existing)
            return replace(shared, via_dedup=True)

        task = asyncio.ensure_future(factory())
        self._inflight[key] = task  # registered before any await
        self.stats.started += 1
        try:
            return await asyncio.shield(task)
        finally:
            if self._inflight.get(key) is task:
                del self._inflight[key]


class FakeClock:
    """Virtual clock for tests: records delays, never really waits."""

    def __init__(self, yield_control: bool = True) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []
        self._yield_control = yield_control

    async def sleep(self, delay: float) -> None:
        if delay < 0:
            raise ValueError(f"negative delay: {delay}")
        self.sleeps.append(delay)
        self.now += delay
        if self._yield_control:
            # One scheduler turn so concurrent callers can attach/detach.
            await asyncio.sleep(0)
