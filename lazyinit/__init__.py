"""Thread-safe lazy initialization (Python 3, standard library only).

Two implementations are provided:

- ``Lazy``       -- double-checked locking. After the value is published,
                    ``get()`` takes the fast path and acquires no lock.
- ``LazyLocked`` -- conservative variant that acquires the lock on every
                    ``get()`` call. Used as the "locked" baseline in the
                    benchmark.

Both guarantee:

- the factory runs at most once per successful initialization, no matter
  how many threads race;
- other threads never observe a partially constructed object: the object
  is fully built by the factory first, and only then published under the
  lock (release) / read under the lock or via the GIL-atomic reference
  load (acquire);
- re-entrant initialization is detected: if the factory itself calls
  ``get()`` on the same instance, ``RecursiveInitializationError`` is
  raised instead of deadlocking.

Failure semantics are configurable via ``FailurePolicy``:

- ``FailurePolicy.RETRY``      -- a failed attempt is forgotten; the next
                                  ``get()`` runs the factory again.
- ``FailurePolicy.PERMANENT``  -- the first exception is latched and
                                  re-raised by every subsequent ``get()``
                                  until ``reset()`` is called explicitly.
"""

from .lazy import (
    FailurePolicy,
    InitializationError,
    InitializationFailedError,
    Lazy,
    LazyLocked,
    RecursiveInitializationError,
)

__all__ = [
    "FailurePolicy",
    "InitializationError",
    "InitializationFailedError",
    "Lazy",
    "LazyLocked",
    "RecursiveInitializationError",
]
