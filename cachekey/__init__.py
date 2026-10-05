"""Header-based cache key construction with collision-resistant normalization.

Public API:
    build_cache_key(headers, ...) -> CacheKey
    KeyBuilder(...).build(headers) -> CacheKey

See docs/NORMALIZATION.md for the full normalization specification.
"""

from .core import (
    CacheKey,
    KeyBuilder,
    KeyTooLongError,
    HeaderError,
    DEFAULT_PARTICIPATING,
    DEFAULT_POLICIES,
    MAX_CANONICAL_BYTES,
    build_cache_key,
)

__all__ = [
    "CacheKey",
    "KeyBuilder",
    "KeyTooLongError",
    "HeaderError",
    "DEFAULT_PARTICIPATING",
    "DEFAULT_POLICIES",
    "MAX_CANONICAL_BYTES",
    "build_cache_key",
]
