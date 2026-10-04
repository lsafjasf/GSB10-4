"""Idempotency-key library (Python 3 standard library only).

Design summary (full spec in docs/KEY_RULES.md):

* A stored record is addressed by the *composite* key
  ``(user_id, endpoint, key)`` -- never by the client-supplied key alone --
  so different users and different endpoints can never overwrite each other.
* The client key must match ``^[A-Za-z0-9_-]{8,128}$``; anything else is
  rejected with 400 before any state is touched.
* Each request carries a *fingerprint* (SHA-256 of method + path + body).
  Replaying a key with a different fingerprint is a conflict (409),
  distinct from expiry (410).
* A record is a state machine:  in_progress -> completed -> expired.
  ``expired`` is terminal: a replay of an expired key is answered 410 and
  is NEVER treated as a first request again.
* While the first request is still running, concurrent duplicates are
  handled by an explicit policy: WAIT (default), REJECT or IN_PROGRESS.
* Completed responses are replayed byte-for-byte: identical status code
  and identical body.  Replays additionally carry the header
  ``Idempotency-Replayed: true`` (a header, never part of the body).
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple

__all__ = [
    "IdempotencyStore",
    "InProgressPolicy",
    "InvalidKeyError",
    "KeyConflictError",
    "KeyExpiredError",
    "InProgressError",
    "WaitTimeoutError",
    "Response",
    "KEY_PATTERN",
    "MIN_KEY_LEN",
    "MAX_KEY_LEN",
    "DEFAULT_TTL_SECONDS",
]

MIN_KEY_LEN = 8
MAX_KEY_LEN = 128
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24h

REPLAYED_HEADER = "Idempotency-Replayed"

# Stable error codes embedded in machine-generated error bodies.
ERR_INVALID_KEY = "IDEMPOTENCY_KEY_INVALID"
ERR_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
ERR_KEY_EXPIRED = "IDEMPOTENCY_KEY_EXPIRED"
ERR_IN_PROGRESS = "IDEMPOTENCY_REQUEST_IN_PROGRESS"
ERR_WAIT_TIMEOUT = "IDEMPOTENCY_WAIT_TIMEOUT"


class IdempotencyError(Exception):
    """Base class; carries the HTTP status the server should answer."""

    status_code = 500
    error_code = "IDEMPOTENCY_ERROR"


class InvalidKeyError(IdempotencyError):
    status_code = 400
    error_code = ERR_INVALID_KEY


class KeyConflictError(IdempotencyError):
    """Same key, different request fingerprint."""

    status_code = 409
    error_code = ERR_KEY_CONFLICT


class KeyExpiredError(IdempotencyError):
    """Key passed its TTL; replaying it must not re-execute the handler."""

    status_code = 410
    error_code = ERR_KEY_EXPIRED


class InProgressError(IdempotencyError):
    """First request still running and policy is REJECT."""

    status_code = 409
    error_code = ERR_IN_PROGRESS


class WaitTimeoutError(IdempotencyError):
    """Policy WAIT gave up waiting for the in-flight first request."""

    status_code = 409
    error_code = ERR_WAIT_TIMEOUT


class InProgressPolicy(Enum):
    WAIT = "wait"            # block until the first request finishes (default)
    REJECT = "reject"        # answer 409 IDEMPOTENCY_REQUEST_IN_PROGRESS
    IN_PROGRESS = "in_progress"  # answer 202 Accepted immediately


@dataclass(frozen=True, slots=True)
class Response:
    """What the server sends back. Replays are byte-identical."""

    status: int
    body: bytes
    headers: Tuple[Tuple[str, str], ...] = ()


class _State(Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    EXPIRED = "expired"


@dataclass(slots=True)
class _Record:
    fingerprint: str
    state: _State
    created_at: float
    expires_at: float
    response: Optional[Response] = None
    condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.Lock())
    )


# Composite store key: (user_id, endpoint, key).  The tuple is used as the
# dict key directly, so scoping is structural and cannot be flattened away.
_ScopedKey = Tuple[str, str, str]


def validate_key(key: object) -> str:
    """Enforce the key format. Raises InvalidKeyError (HTTP 400)."""
    if not isinstance(key, str):
        raise InvalidKeyError("idempotency key must be a string")
    if not (MIN_KEY_LEN <= len(key) <= MAX_KEY_LEN):
        raise InvalidKeyError(
            f"idempotency key length must be {MIN_KEY_LEN}..{MAX_KEY_LEN}"
        )
    if not KEY_PATTERN.match(key):
        raise InvalidKeyError(
            "idempotency key must match [A-Za-z0-9_-]+"
        )
    return key


def request_fingerprint(method: str, path: str, body: bytes) -> str:
    """SHA-256 over a canonical, unambiguous encoding of the request."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"v1\n{method.upper()}\n{path}\n{body_hash}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """Thread-safe, in-memory idempotency registry.

    Parameters
    ----------
    ttl_seconds:
        Lifetime of a *completed* record.  After the TTL the record
        transitions to EXPIRED and replays are answered 410.
    in_progress_policy:
        What to do when a duplicate arrives while the first request is
        still executing.  Default: WAIT.
    wait_timeout_seconds:
        Max time a WAIT-policy duplicate blocks before getting 409
        IDEMPOTENCY_WAIT_TIMEOUT.  None means wait forever.
    clock:
        Injectable time source (monotonic-ish), used by tests to simulate
        TTL expiry without sleeping.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        in_progress_policy: InProgressPolicy = InProgressPolicy.WAIT,
        wait_timeout_seconds: Optional[float] = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl_seconds = ttl_seconds
        self.in_progress_policy = in_progress_policy
        self.wait_timeout_seconds = wait_timeout_seconds
        self._clock = clock
        self._records: Dict[_ScopedKey, _Record] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ API

    def execute(
        self,
        user_id: str,
        endpoint: str,
        key: str,
        fingerprint: str,
        handler: Callable[[], Response],
    ) -> Tuple[Response, bool]:
        """Run ``handler`` at most once per (user_id, endpoint, key).

        Returns ``(response, replayed)``.  ``replayed`` is True when the
        response came from the store instead of executing ``handler``.
        Raises IdempotencyError subclasses for 400/409/410 situations.
        """
        validate_key(key)
        if not user_id or not endpoint:
            raise InvalidKeyError("user_id and endpoint must be non-empty")
        scoped = (user_id, endpoint, key)

        record = self._claim(scoped, fingerprint)
        if record is None:
            # We are the first request: run the handler exactly once.
            return self._run_handler(scoped, handler), False
        # A record already existed; _claim either returned a completed
        # record to replay, or raised, or (WAIT) returned the record that
        # the in-flight request just completed.
        return self._replay(record), True

    # ------------------------------------------------------------- internals

    def _claim(self, scoped: _ScopedKey, fingerprint: str) -> Optional[_Record]:
        """Register or resolve a record.

        Returns None  -> caller must execute the handler (record created).
        Returns record -> caller must replay record.response.
        Raises for conflict / expired / in-progress(REJECT) / wait-timeout.
        """
        deadline = None
        if self.wait_timeout_seconds is not None:
            deadline = self._clock() + self.wait_timeout_seconds

        while True:
            with self._lock:
                record = self._records.get(scoped)
                now = self._clock()

                if record is None:
                    record = _Record(
                        fingerprint=fingerprint,
                        state=_State.IN_PROGRESS,
                        created_at=now,
                        expires_at=now + self.ttl_seconds,
                    )
                    self._records[scoped] = record
                    return None

                if record.state is _State.COMPLETED:
                    if now >= record.expires_at:
                        record.state = _State.EXPIRED
                        record.response = None  # free payload memory
                        raise KeyExpiredError(
                            "idempotency key expired; replay rejected"
                        )
                    if record.fingerprint != fingerprint:
                        raise KeyConflictError(
                            "idempotency key reused with a different request"
                        )
                    return record

                if record.state is _State.EXPIRED:
                    raise KeyExpiredError(
                        "idempotency key expired; replay rejected"
                    )

                # state == IN_PROGRESS
                if record.fingerprint != fingerprint:
                    raise KeyConflictError(
                        "idempotency key reused with a different request"
                    )
                policy = self.in_progress_policy
                if policy is InProgressPolicy.REJECT:
                    raise InProgressError(
                        "request with this idempotency key is still running"
                    )
                if policy is InProgressPolicy.IN_PROGRESS:
                    return _synthetic_in_progress_record()
                # WAIT: block on the record's condition outside the global lock.
                cond = record.condition

            timeout = None if deadline is None else max(0.0, deadline - self._clock())
            with cond:
                notified = cond.wait(timeout=timeout)
            if not notified:
                raise WaitTimeoutError(
                    "timed out waiting for the in-flight request"
                )
            # Loop around and re-inspect the record under the global lock.

    def _run_handler(
        self, scoped: _ScopedKey, handler: Callable[[], Response]
    ) -> Response:
        record = self._records[scoped]
        try:
            response = handler()
            if not isinstance(response, Response):
                raise TypeError("handler must return a Response")
        except BaseException:
            # Handler blew up: nothing cacheable was produced.  Evict the
            # record so the client's retry is treated as a fresh first
            # request instead of being stuck on a phantom in-progress key.
            with self._lock:
                if self._records.get(scoped) is record:
                    del self._records[scoped]
            with record.condition:
                record.condition.notify_all()
            raise

        with self._lock:
            record.state = _State.COMPLETED
            record.response = response
            record.expires_at = self._clock() + self.ttl_seconds
        with record.condition:
            record.condition.notify_all()
        return response

    @staticmethod
    def _replay(record: _Record) -> Response:
        assert record.response is not None
        return record.response

    # ------------------------------------------------------------- maintenance

    def sweep(self) -> int:
        """Drop EXPIRED records (and transition overdue COMPLETED ones).

        Expiry is otherwise lazy (on access); call this periodically to
        bound memory.  Returns the number of records removed.
        """
        removed = 0
        now = self._clock()
        with self._lock:
            for scoped, record in list(self._records.items()):
                if record.state is _State.COMPLETED and now >= record.expires_at:
                    record.state = _State.EXPIRED
                    record.response = None
                if record.state is _State.EXPIRED:
                    del self._records[scoped]
                    removed += 1
        return removed

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


def _synthetic_in_progress_record() -> _Record:
    """Standalone record carrying the 202 answer for IN_PROGRESS policy."""
    return _Record(
        fingerprint="",
        state=_State.COMPLETED,
        created_at=0.0,
        expires_at=float("inf"),
        response=Response(
            status=202,
            body=b'{"error":{"code":"%s","message":"request is being processed"}}'
            % ERR_IN_PROGRESS.encode(),
        ),
    )
