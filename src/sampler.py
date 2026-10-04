"""Reproducible random sampling, Python 3 standard library only.

Algorithm: keyed BLAKE2b hash-threshold (Bernoulli) sampling.

For every input key k and fixed seed s compute

    h(k, s) = BLAKE2b-64(domain, key=SHA256(s)) interpreted as uint64
    keep k  <=>  h(k, s) < rate * 2**64

BLAKE2b output is treated as uniformly distributed in [0, 2**64), so each
key is independently kept with probability exactly `rate`.  The decision for
a key depends only on (key, seed, rate), never on input order or which shard
the key lives on.  Identical keys always receive identical decisions (this is
a feature for join-consistent sampling; duplicate-heavy inputs are covered by
the self-test).
"""
from __future__ import annotations

import hashlib
from typing import Iterable, Iterator, Union

Seed = Union[int, str, bytes]

# Domain-separation prefix so this sampler never collides with other uses
# of keyed BLAKE2b and future versioned algorithms.
_DOMAIN = b"gsb-sampler-v1\x00"
_MOD = 1 << 64


def seed_to_bytes(seed: Seed) -> bytes:
    """Normalize a seed to bytes.

    int  -> unsigned big-endian bytes
    str  -> UTF-8 bytes
    bytes-> unchanged
    """
    if isinstance(seed, bool):
        raise TypeError("seed must be int, str or bytes, not bool")
    if isinstance(seed, int):
        if seed < 0:
            raise ValueError("int seed must be non-negative")
        length = max(1, (seed.bit_length() + 7) // 8)
        return seed.to_bytes(length, "big")
    if isinstance(seed, str):
        return seed.encode("utf-8")
    if isinstance(seed, bytes):
        return seed
    raise TypeError("seed must be int, str or bytes")


def _key_for_seed(seed: Seed) -> bytes:
    """BLAKE2b keys are limited to 64 bytes; hash arbitrary seeds to 32."""
    raw = seed_to_bytes(seed)
    if len(raw) <= 64:
        return raw
    return hashlib.sha256(raw).digest()


class Sampler:
    """Deterministic, order-independent hash sampler.

    Parameters
    ----------
    seed : int | str | bytes
        Any non-negative integer / string / byte string.  Same seed always
        gives the same decisions.
    rate : float
        Sampling probability in [0.0, 1.0].  0.0 keeps nothing, 1.0 keeps
        every key.
    """

    def __init__(self, seed: Seed, rate: float):
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            raise TypeError("rate must be a number in [0.0, 1.0]")
        if not (0.0 <= rate <= 1.0):
            raise ValueError("rate must be in [0.0, 1.0]")
        self.seed = seed
        self.rate = rate
        # rate == 1.0 -> threshold == 2**64, every 64-bit hash is below it.
        # rate == 0.0 -> threshold == 0, no hash is below it.
        self.threshold = int(rate * _MOD)
        self._template = hashlib.blake2b(
            _DOMAIN, digest_size=8, key=_key_for_seed(seed)
        )

    def hash64(self, key: Union[bytes, bytearray, str]) -> int:
        """Return the 64-bit hash of `key` under this sampler's seed."""
        if isinstance(key, str):
            key = key.encode("utf-8")
        elif isinstance(key, bytearray):
            key = bytes(key)
        elif not isinstance(key, bytes):
            raise TypeError("key must be bytes or str")
        h = self._template.copy()
        h.update(key)
        return int.from_bytes(h.digest(), "big")

    def keep(self, key: Union[bytes, str]) -> bool:
        """Return True iff `key` belongs to the sample."""
        return self.hash64(key) < self.threshold

    def filter(self, keys: Iterable[Union[bytes, str]]) -> Iterator[Union[bytes, str]]:
        """Stream keys, yielding exactly those that are sampled."""
        for key in keys:
            if self.keep(key):
                yield key


def sample_lines(lines: Iterable[bytes], seed: Seed, rate: float) -> Iterator[bytes]:
    """Convenience wrapper for newline-delimited byte records."""
    return Sampler(seed, rate).filter(lines)
