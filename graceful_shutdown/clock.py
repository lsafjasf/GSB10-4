"""Injectable time source.

All shutdown deadlines are measured through a Clock so that tests can
deterministically fast-forward time while production code uses the wall
clock.
"""

from __future__ import annotations

import threading
import time


class SystemClock:
    """Wall-clock time source used in production."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


class FakeClock:
    """Deterministic clock for tests.

    ``sleep()`` advances virtual time instantly and only yields a tiny
    amount of real wall time so that real worker threads can make
    progress. Reported timings therefore come entirely from virtual time.
    """

    def __init__(self, start: float = 1000.0, real_yield: float = 0.0005) -> None:
        self._now = float(start)
        self.real_yield = real_yield
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._now += max(0.0, seconds)
        if self.real_yield > 0:
            time.sleep(self.real_yield)
