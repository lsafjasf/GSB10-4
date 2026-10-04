"""
Idempotency key store — Python 3 standard library only.

Key model
---------
A request is uniquely identified by a *scoped key*:

    key = BLAKE2b-128( len(user_id) || user_id
                       || len(endpoint) || endpoint
                       || len(client_key) || client_key )

- user_id  : different users never share a key (even with the same client key)
- endpoint : different interfaces never share a key
- client_key: the value the client supplies (e.g. UUIDv4), length-prefixed
              so ("ab","c") / ("a","bc") style concatenation cannot collide

The request *payload fingerprint* is bound to the key as well:

    fingerprint = BLAKE2b-128(canonical request body bytes)

The same key arriving with a different fingerprint is a conflict, not a retry.

Record lifecycle
----------------
    (none) --begin--> IN_PROGRESS --complete(status,body)--> COMPLETED
                        |                                       |
                     abort()                            ttl reached
                        |                                       v
                        +--> (none)                          EXPIRED
                                                              (tombstone:
                                                               fingerprint
                                                               kept, body
                                                               dropped)
                                                       tombstone_ttl later
                                                              v
                                                            (none)

Decisions returned by begin():
  EXECUTE       first sight of this key (or tombstone fully purged)
  REPLAY        a stored response exists -> return status+body byte-identical
  IN_PROGRESS   another request with this key is still running
  CONFLICT      same key, different payload fingerprint
  EXPIRED       key existed, TTL elapsed; replay must NOT be executed again
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from enum import Enum
from typing import Any, Callable, Optional, Tuple

DIGEST_SIZE = 16  # 128-bit. Birthday collision for 10^6 keys: ~1.5e-27.

_IN_PROGRESS = 0
_COMPLETED = 1
_EXPIRED = 2


class Decision(str, Enum):
    EXECUTE = "execute"
    REPLAY = "replay"
    IN_PROGRESS = "in_progress"
    CONFLICT = "conflict"
    EXPIRED = "expired"


def make_key(user_id: str, endpoint: str, client_key: str) -> bytes:
    """Build a collision-resistant, scope-isolated idempotency key."""
    h = hashlib.blake2b(digest_size=DIGEST_SIZE)
    for part in (user_id, endpoint, client_key):
        b = part.encode("utf-8")
        h.update(len(b).to_bytes(4, "big"))
        h.update(b)
    return h.digest()


def fingerprint_payload(body: bytes) -> bytes:
    """Fingerprint raw request body bytes (must be canonicalized upstream)."""
    return hashlib.blake2b(body, digest_size=DIGEST_SIZE).digest()


def fingerprint_json(obj: Any) -> bytes:
    """Fingerprint a JSON body with stable canonical serialization."""
    canonical = json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return fingerprint_payload(canonical)


class BeginResult:
    __slots__ = ("decision", "status", "body")

    def __init__(
        self,
        decision: Decision,
        status: Optional[int] = None,
        body: Optional[bytes] = None,
    ) -> None:
        self.decision = decision
        self.status = status
        self.body = body


class _Record:
    __slots__ = (
        "state",
        "fingerprint",
        "status",
        "body",
        "expires_at",
        "tombstone_until",
    )

    def __init__(
        self,
        state: int,
        fingerprint: bytes,
        status: Optional[int],
        body: Optional[bytes],
        expires_at: float,
        tombstone_until: float,
    ) -> None:
        self.state = state
        self.fingerprint = fingerprint
        self.status = status
        self.body = body
        self.expires_at = expires_at
        self.tombstone_until = tombstone_until


class IdempotencyStore:
    """Thread-safe in-memory idempotency store.

    ttl:            how long a COMPLETED response is replayable (seconds)
    tombstone_ttl:  how long an EXPIRED key is remembered after TTL, so a
                    late replay is refused (EXPIRED) instead of re-executed
    wait_timeout:   how long a concurrent duplicate waits for the first
                    request to finish before getting IN_PROGRESS
    clock:          injectable monotonic clock (seconds) for testing
    """

    def __init__(
        self,
        ttl: float = 24 * 3600.0,
        tombstone_ttl: float = 7 * 24 * 3600.0,
        wait_timeout: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl <= 0 or tombstone_ttl < 0 or wait_timeout < 0:
            raise ValueError("ttl/tombstone_ttl/wait_timeout out of range")
        self._ttl = ttl
        self._tombstone_ttl = tombstone_ttl
        self._wait_timeout = wait_timeout
        self._clock = clock
        self._cond = threading.Condition()
        self._records: dict[bytes, _Record] = {}

    def begin(
        self,
        key: bytes,
        fingerprint: bytes,
        wait: bool = True,
    ) -> BeginResult:
        """Start an idempotency transaction. See module docs for decisions."""
        now = self._clock()
        deadline = now + self._wait_timeout
        with self._cond:
            while True:
                rec = self._records.get(key)
                if rec is not None:
                    self._maybe_expire_locked(rec, now)

                if rec is None or (
                    rec.state == _EXPIRED and now >= rec.tombstone_until
                ):
                    self._records[key] = _Record(
                        _IN_PROGRESS,
                        fingerprint,
                        None,
                        None,
                        now + self._ttl,
                        0.0,
                    )
                    return BeginResult(Decision.EXECUTE)

                if rec.state == _EXPIRED:
                    return BeginResult(Decision.EXPIRED)

                if rec.fingerprint != fingerprint:
                    return BeginResult(Decision.CONFLICT)

                if rec.state == _COMPLETED:
                    return BeginResult(Decision.REPLAY, rec.status, rec.body)

                if not wait:
                    return BeginResult(Decision.IN_PROGRESS)

                remaining = deadline - now
                if remaining <= 0:
                    return BeginResult(Decision.IN_PROGRESS)
                self._cond.wait(remaining)
                now = self._clock()

    def complete(
        self,
        key: bytes,
        fingerprint: bytes,
        status: int,
        body: bytes,
    ) -> bool:
        """Finish the first request. Only the owning begin() call succeeds.

        Returning True means the response is now stored for replay. Store the
        real status even for 4xx/5xx so retries replay the same failure;
        use abort() only when nothing actually happened.
        """
        now = self._clock()
        with self._cond:
            rec = self._records.get(key)
            if (
                rec is None
                or rec.state != _IN_PROGRESS
                or rec.fingerprint != fingerprint
            ):
                return False
            rec.state = _COMPLETED
            rec.status = int(status)
            rec.body = bytes(body)
            rec.expires_at = now + self._ttl
            self._cond.notify_all()
            return True

    def abort(self, key: bytes, fingerprint: bytes) -> bool:
        """Release an IN_PROGRESS slot (e.g. crash before any side effect).

        A subsequent retry with the same key is then allowed to EXECUTE.
        """
        with self._cond:
            rec = self._records.get(key)
            if (
                rec is None
                or rec.state != _IN_PROGRESS
                or rec.fingerprint != fingerprint
            ):
                return False
            del self._records[key]
            self._cond.notify_all()
            return True

    def purge_past_tombstones(self) -> int:
        """Free fully-elapsed tombstones. Returns number removed.

        Call periodically (e.g. per minute) from a maintenance thread; only
        dead tombstones are removed, live keys are untouched.
        """
        now = self._clock()
        with self._cond:
            dead = [
                k
                for k, r in self._records.items()
                if r.state == _EXPIRED and now >= r.tombstone_until
            ]
            for k in dead:
                del self._records[k]
        return len(dead)

    def __len__(self) -> int:
        with self._cond:
            return len(self._records)

    def _maybe_expire_locked(self, rec: _Record, now: float) -> None:
        if rec.state != _EXPIRED and now >= rec.expires_at:
            rec.state = _EXPIRED
            rec.status = None
            rec.body = None
            rec.tombstone_until = rec.expires_at + self._tombstone_ttl


# --- suggested HTTP glue --------------------------------------------------

# Mapping for decisions that should not reach the real handler.
HTTP_FOR_DECISION = {
    Decision.IN_PROGRESS: (409, b'{"error":"request_in_progress"}'),
    Decision.CONFLICT: (422, b'{"error":"idempotency_key_conflict"}'),
    Decision.EXPIRED: (410, b'{"error":"idempotency_key_expired"}'),
}


def handle_once(
    store: IdempotencyStore,
    key: bytes,
    fingerprint: bytes,
    handler: Callable[[], Tuple[int, bytes]],
) -> Tuple[int, bytes]:
    """Run handler at most once per key; replay the exact stored response.

    - First request      : handler runs, response stored, returned.
    - Duplicate (later)  : stored (status, body) returned byte-identical.
    - Concurrent dup     : waits for the first call, then replays it;
                           after wait_timeout it gets HTTP 409.
    - Key/payload clash  : HTTP 422 (same key, different body).
    - Expired replay     : HTTP 410 (must not execute again).
    """
    result = store.begin(key, fingerprint)
    if result.decision is Decision.EXECUTE:
        status, body = handler()
        if not store.complete(key, fingerprint, status, body):
            return HTTP_FOR_DECISION[Decision.IN_PROGRESS]
        return status, bytes(body)
    if result.decision is Decision.REPLAY:
        return result.status, result.body
    return HTTP_FOR_DECISION[result.decision]
