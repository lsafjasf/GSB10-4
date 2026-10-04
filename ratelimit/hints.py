"""Retry hints: base delay quantization, floor guard, reproducible jitter.

Floor rule (hard requirement): a hint must never be smaller than the actual
remaining time, otherwise clients retry too early and stampede the limiter
again at the same instant.

Layer 1 -- quantization: base_delay_ms = ceil(remaining_raw * 1000 - 1e-9).
    ceil() can never undercut its argument; the 1e-9 epsilon only avoids
    snapping an exact integer like 3.0000000000000004 up a whole extra
    millisecond due to float representation noise.

Layer 2 -- runtime floor guard (defense in depth): every emitted hint is
    checked against floor_ms = ceil(remaining_raw * 1000 - 1e-9), i.e. the
    smallest whole millisecond not below the actual remaining time. If a
    (buggy or custom) computation undercuts, the hint is clamped up to the
    floor, the violation is counted, and in strict mode HintFloorViolation
    is raised.

Jitter: hint = base + U[0, spread_ms], spread >= 0, drawn from a seeded
SplitMix64. Adding a non-negative offset can never break the floor rule.
Same seed + same spread + same call sequence -> identical hints.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .rng import SplitMix64

SNAP_EPS = 1e-9        # float representation snap tolerance (seconds)
DEFAULT_SEED = 0x5EED5EED5EED5EED
DEFAULT_SPREAD_MS = 1000


class HintFloorViolation(RuntimeError):
    """A computed hint was smaller than the actual remaining time."""


def base_delay_ms(remaining_raw: float) -> int:
    """Smallest whole millisecond not below remaining_raw (layer 1)."""
    if remaining_raw <= 0.0:
        return 0
    return math.ceil(remaining_raw * 1000.0 - SNAP_EPS)


def floor_ms(remaining_raw: float) -> int:
    """Minimum legal hint in whole milliseconds (guard reference)."""
    return base_delay_ms(remaining_raw)


@dataclass(frozen=True)
class JitterSuggestion:
    base_ms: int
    jitter_ms: int
    hint_ms: int
    floor_ms: int
    clamped: bool          # True if the guard had to repair an undercut
    jitter_index: int      # how many jitter draws so far (reproducibility aid)


class JitterSuggester:
    """hint = ceil-quantized base + U[0, spread_ms], guarded against undercut."""

    def __init__(
        self,
        seed: int = DEFAULT_SEED,
        spread_ms: int = DEFAULT_SPREAD_MS,
        strict: bool = False,
    ) -> None:
        if spread_ms < 0:
            raise ValueError("spread_ms must be >= 0")
        self.seed = int(seed)
        self.spread_ms = int(spread_ms)
        self.strict = strict
        self._rng = SplitMix64(self.seed)
        self.jitter_index = 0
        self.clamped_count = 0

    def suggest(self, remaining_raw: float) -> JitterSuggestion:
        base = base_delay_ms(remaining_raw)
        jitter = self._rng.uniform_millis(self.spread_ms)
        self.jitter_index += 1
        hint = base + jitter
        floor = floor_ms(remaining_raw)
        clamped = False
        if hint < floor:  # layer 2: should be unreachable for built-in math
            clamped = True
            self.clamped_count += 1
            if self.strict:
                raise HintFloorViolation(
                    f"hint {hint}ms < floor {floor}ms "
                    f"(remaining_raw={remaining_raw!r})"
                )
            hint = floor
        return JitterSuggestion(
            base_ms=base,
            jitter_ms=jitter,
            hint_ms=hint,
            floor_ms=floor,
            clamped=clamped,
            jitter_index=self.jitter_index,
        )


def guard_hint(hint_ms: int, remaining_raw: float, strict: bool = False) -> int:
    """Standalone guard for externally computed hints (e.g. Retry-After)."""
    floor = floor_ms(remaining_raw)
    if hint_ms < floor:
        if strict:
            raise HintFloorViolation(
                f"hint {hint_ms}ms < floor {floor}ms "
                f"(remaining_raw={remaining_raw!r})"
            )
        return floor
    return hint_ms
