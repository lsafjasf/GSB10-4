"""Thread-safe lazy initialization primitives.

Memory-visibility reasoning (CPython reference implementation):

1. Every object observed by another thread is constructed *inside* the
   factory while the initializing thread holds the lock; the fully built
   object is only assigned to ``self._value`` after construction returns.
2. The assignment happens while the lock is held, and the lock is released
   with the value already stored. Lock release is a release operation; a
   subsequent lock acquire by another thread is an acquire operation, so
   the happens-before chain publishes the complete object together with
   all of its fields.
3. The fast path in ``Lazy.get`` reads ``self._value`` once, without the
   lock. In CPython a single attribute load of a reference is atomic with
   respect to the GIL (every bytecode step and every attribute store is
   visible either wholly before or wholly after a GIL switch), and the
   value points either to the sentinel ``_UNSET`` or to a fully
   constructed object -- never to a half-built one, because the object
   reference is not published until construction finishes. The "locked"
   slow path additionally provides acquire/release synchronization for
   callers that require lock-based guarantees (e.g. non-CPython runtimes
   or conservative reasoning).
"""

from __future__ import annotations

import enum
import threading
from typing import Any, Callable, Optional

_UNSET = object()


class FailurePolicy(enum.Enum):
    """What happens after the factory raises an exception."""

    RETRY = "retry"
    PERMANENT = "permanent"


class InitializationError(Exception):
    """Base class for lazy-initialization errors."""


class InitializationFailedError(InitializationError):
    """Wraps an exception raised by the factory.

    The original exception is available as ``__cause__``.
    """


class RecursiveInitializationError(InitializationError):
    """Raised when the factory re-entrantly calls ``get`` on itself."""


class _LazyBase:
    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        failure_policy: FailurePolicy = FailurePolicy.RETRY,
        name: Optional[str] = None,
    ) -> None:
        if not callable(factory):
            raise TypeError("factory must be callable")
        self._factory = factory
        self._failure_policy = FailurePolicy(failure_policy)
        self._name = name or getattr(factory, "__name__", "lazy")
        self._lock = threading.RLock()
        self._value: Any = _UNSET
        self._error: Optional[BaseException] = None
        self._initializing = False
        # Statistics: total attempts, successful inits, failed attempts.
        self._attempts = 0
        self._successes = 0
        self._failures = 0

    # -- introspection --------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> str:
        """One of ``unset``, ``initializing``, ``ready``, ``failed``."""
        with self._lock:
            if self._value is not _UNSET:
                return "ready"
            if self._error is not None:
                return "failed"
            if self._initializing:
                return "initializing"
            return "unset"

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "attempts": self._attempts,
                "successes": self._successes,
                "failures": self._failures,
            }

    def is_initialized(self) -> bool:
        return self.state == "ready"

    def peek(self):
        """Return the value if ready, otherwise ``_UNSET`` without blocking."""
        return self._value

    def reset(self) -> None:
        """Forget the value (or latched error); the next ``get`` re-inits."""
        with self._lock:
            if self._initializing:
                raise RecursiveInitializationError(
                    f"{self._name}: cannot reset while initialization is running"
                )
            self._value = _UNSET
            self._error = None

    # -- slow path, shared by both implementations ----------------------

    def _initialize_locked(self):
        # Called with the lock held.
        if self._initializing:
            raise RecursiveInitializationError(
                f"{self._name}: factory recursively called get() during its own "
                "initialization"
            )
        self._initializing = True
        self._attempts += 1
        try:
            value = self._factory()
        except RecursiveInitializationError:
            # Re-entrant access from the factory itself: do not treat it
            # as a factory failure, and do not wrap it.
            raise
        except Exception as exc:
            self._failures += 1
            if self._failure_policy is FailurePolicy.PERMANENT:
                self._error = exc
            # RETRY: leave state unset so the next get() tries again.
            raise InitializationFailedError(
                f"{self._name}: factory raised {type(exc).__name__}: {exc}"
            ) from exc
        else:
            # Publish only after construction has fully returned.
            self._value = value
            self._error = None
            self._successes += 1
            return value
        finally:
            self._initializing = False


class Lazy(_LazyBase):
    """Double-checked locking: lock-free fast path after publication."""

    def get(self):
        value = self._value  # one atomic reference load on CPython
        if value is not _UNSET:
            return value
        # Snapshot the latched permanent error cheaply as well.
        error = self._error
        if error is not None and self._failure_policy is FailurePolicy.PERMANENT:
            raise InitializationFailedError(
                f"{self._name}: permanently failed: "
                f"{type(error).__name__}: {error}"
            ) from error
        with self._lock:
            if self._value is not _UNSET:
                return self._value
            if (
                self._error is not None
                and self._failure_policy is FailurePolicy.PERMANENT
            ):
                raise InitializationFailedError(
                    f"{self._name}: permanently failed: "
                    f"{type(self._error).__name__}: {self._error}"
                ) from self._error
            return self._initialize_locked()


class LazyLocked(_LazyBase):
    """Every ``get`` takes the lock (the locked baseline)."""

    def get(self):
        with self._lock:
            if self._value is not _UNSET:
                return self._value
            if (
                self._error is not None
                and self._failure_policy is FailurePolicy.PERMANENT
            ):
                raise InitializationFailedError(
                    f"{self._name}: permanently failed: "
                    f"{type(self._error).__name__}: {self._error}"
                ) from self._error
            return self._initialize_locked()
