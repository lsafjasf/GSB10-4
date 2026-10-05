"""Async retry with jitter + single-flight dedup. Stdlib only.

Design notes
------------
Jitter: "Full Jitter" (AWS Architecture Blog style):
    delay = rand() * min(cap, base * 2**attempt)
  - rand() in [0, 1)  =>  delay in [0, limit), limit = min(cap, base*2**attempt)
  - E[delay] = limit / 2
  - injectable `rand` makes the sequence reproducible (seeded random.Random)
    and lets tests hit the boundary values exactly.

Dedup: single-flight. Concurrent calls with the same `key` share one
in-flight asyncio.Task; late arrivals await the shared task and get an
Outcome with source="deduplicated" (and stats.merged is incremented).

Result discrimination:
  - success (executed)      -> Outcome(source="executed")
  - success (dedup hit)     -> Outcome(source="deduplicated")
  - retries exhausted       -> raises RetryExhausted (never an Outcome)
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

SOURCE_EXECUTED = "executed"
SOURCE_DEDUPLICATED = "deduplicated"


class RetryExhausted(Exception):
    """All attempts used up. Distinct from any successful Outcome."""

    def __init__(self, key: Any, attempts: int, last_error: BaseException):
        self.key = key
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"retry exhausted for key={key!r} after {attempts} attempts: {last_error!r}"
        )


@dataclass(frozen=True)
class Outcome:
    value: Any
    attempts: int          # attempts the *executing* task actually used
    source: str            # SOURCE_EXECUTED | SOURCE_DEDUPLICATED


@dataclass
class DedupStats:
    submitted: int = 0     # total execute() calls
    executed: int = 0      # calls that really ran fn
    merged: int = 0        # duplicate calls folded into an in-flight task


def full_jitter_delay(attempt: int, *, base: float, cap: float,
                      rand: Callable[[], float]) -> float:
    """Full Jitter: rand() * min(cap, base * 2**attempt)."""
    limit = min(cap, base * (2 ** attempt))
    return rand() * limit


class RetryExecutor:
    def __init__(
        self,
        *,
        max_attempts: int = 3,
        base_delay: float = 0.1,
        cap_delay: float = 5.0,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        rand: Optional[Callable[[], float]] = None,
        now: Callable[[], float] = time.monotonic,
        seed: Optional[int] = None,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.cap_delay = cap_delay
        self._sleep = sleep if sleep is not None else asyncio.sleep
        if rand is not None:
            self._rand = rand
        else:
            self._rand = random.Random(seed).random  # seeded => reproducible
        self._now = now
        self._inflight: Dict[Any, asyncio.Task] = {}
        self.stats = DedupStats()

    async def _run(self, key: Any, fn: Callable[[], Awaitable[Any]]) -> Outcome:
        last_error: Optional[BaseException] = None
        for attempt in range(self.max_attempts):
            try:
                value = await fn()
                return Outcome(value=value, attempts=attempt + 1,
                               source=SOURCE_EXECUTED)
            except Exception as exc:  # noqa: BLE001 - retryable by contract
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    delay = full_jitter_delay(
                        attempt, base=self.base_delay,
                        cap=self.cap_delay, rand=self._rand)
                    await self._sleep(delay)
        raise RetryExhausted(key, self.max_attempts, last_error)

    async def execute(self, key: Any,
                      fn: Callable[[], Awaitable[Any]]) -> Outcome:
        self.stats.submitted += 1
        task = self._inflight.get(key)
        if task is not None:
            self.stats.merged += 1
            # shield: a cancelled waiter must not cancel the shared task
            outcome = await asyncio.shield(task)
            return Outcome(value=outcome.value, attempts=outcome.attempts,
                           source=SOURCE_DEDUPLICATED)
        self.stats.executed += 1
        task = asyncio.ensure_future(self._run(key, fn))
        self._inflight[key] = task
        try:
            return await task
        finally:
            self._inflight.pop(key, None)
