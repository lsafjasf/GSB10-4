"""Retry policy: exponential backoff with bounded uniform jitter.

Jitter is drawn from an injected random.Random, so a fixed seed reproduces the
exact same schedule -- required for deterministic tests and audit replays.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5  # total deliveries, including the first one
    base_delay: float = 1.0  # seconds
    multiplier: float = 2.0
    max_delay: float = 60.0  # cap on the un-jittered backoff
    jitter_ratio: float = 0.2  # +/- 20% uniform jitter

    def base_for(self, attempt: int) -> float:
        """Un-jittered delay after the given 1-based attempt failed."""
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        return min(self.base_delay * self.multiplier ** (attempt - 1), self.max_delay)

    def delay(self, attempt: int, rng: random.Random) -> float:
        base = self.base_for(attempt)
        spread = base * self.jitter_ratio
        return base + rng.uniform(-spread, spread)
