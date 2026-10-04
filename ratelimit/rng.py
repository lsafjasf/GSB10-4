"""Deterministic PRNG for reproducible jitter.

SplitMix64 (Sebastiano Vigna): 64-bit state, splitmix64 mixing. It is
integer-only, has no platform randomness, and the same seed always yields
the same stream. Bounded integers use Lemire's nearly-divisionless method
with rejection, which is *unbiased* (no modulo bias).

Seed 0 is allowed: state is initialized to seed and each next() call
performs the splitmix increment+mix, so identical seeds -> identical output.
"""
from __future__ import annotations

import math

MASK64 = (1 << 64) - 1


class SplitMix64:
    def __init__(self, seed: int = 0) -> None:
        self.state = int(seed) & MASK64

    def next64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return z ^ (z >> 31)

    def bounded(self, n: int) -> int:
        """Uniform integer in [0, n) via Lemire rejection; n >= 1."""
        if n <= 0:
            raise ValueError("bounded() requires a positive bound")
        threshold = (MASK64 - n + 1) % n
        while True:
            x = self.next64()
            m = x * n
            l = m & MASK64
            if l >= threshold:
                return m >> 64

    def uniform_millis(self, span_ms: int) -> int:
        """Uniform integer milliseconds in [0, span_ms] (inclusive)."""
        if span_ms < 0:
            raise ValueError("span_ms must be >= 0")
        return self.bounded(span_ms + 1)
