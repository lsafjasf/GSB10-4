"""Bounded sliding-window acknowledgement tracker (standard library only).

The publisher assigns monotonically increasing sequence numbers.  The window
has a hard capacity measured in sequence slots: when ``window_used`` reaches
``capacity``, publishers pause (block) instead of dropping messages or
disconnecting.  Subscribers acknowledge individual sequence numbers; acks may
arrive out of order and duplicates are idempotent.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set


class AckWindowError(Exception):
    """Base error for ack-window failures."""


class WindowFull(AckWindowError):
    """Raised by non-blocking or timed sends when the window is full."""


class UnknownSequence(AckWindowError):
    """Raised when an ack references a sequence number never sent."""


@dataclass(frozen=True)
class AckResult:
    """Outcome of processing one ack."""

    seq: int
    status: str  # "accepted" or "duplicate"
    base: int  # smallest sequence number not yet cumulatively acknowledged
    released: int  # window slots freed by this ack (0 for duplicates)
    unacked: int  # sent but not yet acknowledged
    window_used: int  # sequence slots occupied: next_seq - base


@dataclass(frozen=True)
class WindowSnapshot:
    capacity: int
    next_seq: int
    base: int
    unacked: int
    window_used: int
    acked_ahead: int


class AckWindow:
    """A bounded sliding window with out-of-order, idempotent ack handling.

    Overflow policy: **pause the publisher**.  ``send()`` blocks until a
    cumulative ack advances the window (or an optional timeout expires);
    ``try_send()`` raises :class:`WindowFull` immediately.  Nothing is dropped
    and the connection is never torn down by this policy.

    Only two structures grow with the window: ``_pending`` (seq -> payload for
    unacknowledged messages) and ``_acked`` (selective acks ahead of ``base``).
    Together they hold at most ``window_used <= capacity`` entries, so memory
    is bounded by the configured capacity.
    """

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self.capacity = capacity
        self._next_seq = 1
        self._base = 1
        self._pending: Dict[int, Any] = {}
        self._acked: Set[int] = set()
        self._closed = False
        self._cv = threading.Condition()
        self._stats = {
            "sent": 0,
            "acks_accepted": 0,
            "acks_duplicate": 0,
            "pauses": 0,
            "pause_wait_seconds": 0.0,
            "advance_events": 0,
            "max_window_used": 0,
            "max_unacked": 0,
        }

    @property
    def next_seq(self) -> int:
        return self._next_seq

    @property
    def base(self) -> int:
        return self._base

    @property
    def unacked(self) -> int:
        return len(self._pending)

    @property
    def window_used(self) -> int:
        return self._next_seq - self._base

    def snapshot(self) -> WindowSnapshot:
        with self._cv:
            return WindowSnapshot(
                capacity=self.capacity,
                next_seq=self._next_seq,
                base=self._base,
                unacked=len(self._pending),
                window_used=self._next_seq - self._base,
                acked_ahead=len(self._acked),
            )

    def stats(self) -> Dict[str, float]:
        with self._cv:
            return dict(self._stats)

    def send(self, payload: Any = None, timeout: Optional[float] = None) -> int:
        """Send one message, blocking while the window is full.

        ``timeout=None`` waits indefinitely; ``timeout=0`` is a non-blocking
        attempt.  Returns the assigned sequence number.
        """
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be >= 0")
        with self._cv:
            self._raise_if_closed()
            deadline = None if timeout is None else time.monotonic() + timeout
            wait_started: Optional[float] = None
            try:
                while self.window_used >= self.capacity:
                    self._raise_if_closed()
                    if wait_started is None:
                        wait_started = time.monotonic()
                    self._stats["pauses"] += 1
                    if deadline is None:
                        self._cv.wait()
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WindowFull(
                            f"window full: {self.window_used}/{self.capacity} slots in use"
                        )
                    self._cv.wait(remaining)
                self._raise_if_closed()
            finally:
                if wait_started is not None:
                    self._stats["pause_wait_seconds"] += time.monotonic() - wait_started

            seq = self._next_seq
            self._next_seq += 1
            self._pending[seq] = payload
            self._stats["sent"] += 1
            self._stats["max_window_used"] = max(
                self._stats["max_window_used"], self.window_used
            )
            self._stats["max_unacked"] = max(
                self._stats["max_unacked"], len(self._pending)
            )
            return seq

    def try_send(self, payload: Any = None) -> int:
        """Non-blocking send; raises :class:`WindowFull` when full."""
        return self.send(payload, timeout=0)

    def ack(self, seq: int) -> AckResult:
        """Process one ack; duplicates are idempotent no-ops."""
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise TypeError("seq must be an int")
        with self._cv:
            if seq < self._base or seq in self._acked:
                self._stats["acks_duplicate"] += 1
                return AckResult(
                    seq=seq,
                    status="duplicate",
                    base=self._base,
                    released=0,
                    unacked=len(self._pending),
                    window_used=self.window_used,
                )
            if seq >= self._next_seq:
                raise UnknownSequence(f"seq {seq} was never sent")

            del self._pending[seq]
            self._stats["acks_accepted"] += 1
            released = 0
            if seq == self._base:
                released = 1
                self._base = seq + 1
                while self._base in self._acked:
                    self._acked.remove(self._base)
                    self._base += 1
                    released += 1
                self._stats["advance_events"] += 1
            else:
                self._acked.add(seq)

            self._cv.notify_all()
            return AckResult(
                seq=seq,
                status="accepted",
                base=self._base,
                released=released,
                unacked=len(self._pending),
                window_used=self.window_used,
            )

    def close(self) -> None:
        """Wake blocked senders; subsequent sends fail with AckWindowError."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def memory_bytes(self) -> int:
        """Approximate bytes held by the unacked-window structures.

        Counts the pending dict, the selective-ack set, and every stored
        sequence number and payload via ``sys.getsizeof``.  Interpreter and
        call-stack overhead are excluded; the number is a reproducible
        structural estimate, not a process RSS figure.
        """
        with self._cv:
            total = sys.getsizeof(self._pending) + sys.getsizeof(self._acked)
            for seq, payload in self._pending.items():
                total += sys.getsizeof(seq) + sys.getsizeof(payload)
            for seq in self._acked:
                total += sys.getsizeof(seq)
            return total

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise AckWindowError("window is closed")
