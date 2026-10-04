"""In-process publish/subscribe with per-subscriber failure isolation.

Design summary
--------------
* Dispatch is **synchronous and serial**: for one ``publish`` call,
  subscribers of that topic are invoked one at a time, in subscription
  order. Concurrent delivery is intentionally *not* performed, so the
  delivery order is fully deterministic.
* Each subscriber call is wrapped in its own ``try/except``; an exception
  raised by one subscriber is captured and never aborts the dispatch loop.
  All failures are aggregated and returned to the publisher (it is also
  possible to raise them as one ``AggregatePublishError``).
* Unsubscribe is effective immediately, including for the dispatch that is
  currently running. The loop iterates a snapshot (so subscribers added
  during a dispatch are not invoked this round) and re-checks the
  subscription's ``active`` flag immediately before every invocation, so a
  handler that unsubscribes itself or a later subscriber guarantees that
  subscriber is not called.
* Handler callbacks are invoked *outside* the internal lock, so a handler
  may legally call ``subscribe``/``unsubscribe`` without deadlocking.
"""

from __future__ import annotations

import itertools

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


@dataclass
class SubscriberError:
    """One captured subscriber failure, with identity and original error."""

    topic: str
    token: int
    subscriber: str
    error: BaseException
    order: int

    def describe(self) -> str:
        return (
            f"[order={self.order}] subscriber token={self.token} "
            f"({self.subscriber}) on topic={self.topic!r} raised "
            f"{type(self.error).__name__}: {self.error}"
        )


@dataclass
class PublishReport:
    """Result of one publish: who got the event and every failure seen."""

    topic: str
    event: Any = None
    delivered: List[int] = field(default_factory=list)
    skipped_unsubscribed: List[int] = field(default_factory=list)
    errors: List[SubscriberError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def delivered_count(self) -> int:
        return len(self.delivered)

    @property
    def failed_count(self) -> int:
        return len(self.errors)

    def describe_errors(self) -> str:
        return "\n".join(item.describe() for item in self.errors)


class AggregatePublishError(Exception):
    """Raised by ``publish(..., raise_on_error=True)``; never hides errors."""

    def __init__(self, report: PublishReport) -> None:
        self.report = report
        super().__init__(
            f"{report.failed_count} subscriber(s) failed for topic "
            f"{report.topic!r}:\n{report.describe_errors()}"
        )


class _Subscription:
    __slots__ = ("token", "topic", "handler", "name", "active")

    def __init__(self, token: int, topic: str, handler: Callable[[Any], None],
                 name: str) -> None:
        self.token = token
        self.topic = topic
        self.handler = handler
        self.name = name
        self.active = True


Handler = Callable[[Any], None]


class EventBus:
    """Thread-safe in-process event bus with serial, isolated dispatch."""

    def __init__(self) -> None:
        self._subs: dict[str, List[_Subscription]] = {}
        self._index: dict[int, _Subscription] = {}
        self._tokens = itertools.count(1)
        self._lock = threading.RLock()

    def subscribe(self, topic: str, handler: Handler, *,
                  name: Optional[str] = None) -> int:
        """Subscribe ``handler`` to ``topic``; returns an opaque token.

        Subscribers are delivered in subscription order.
        """
        sub = _Subscription(
            token=next(self._tokens),
            topic=topic,
            handler=handler,
            name=name or getattr(handler, "__name__", repr(handler)),
        )
        with self._lock:
            self._subs.setdefault(topic, []).append(sub)
            self._index[sub.token] = sub
        return sub.token

    def unsubscribe(self, token: int) -> bool:
        """Cancel a subscription. Effective immediately, even mid-dispatch.

        Returns True if a live subscription was removed, False otherwise.
        """
        with self._lock:
            sub = self._index.pop(token, None)
            if sub is None or not sub.active:
                return False
            sub.active = False
            subs = self._subs.get(sub.topic)
            if subs is not None:
                try:
                    subs.remove(sub)
                except ValueError:
                    pass
                if not subs:
                    self._subs.pop(sub.topic, None)
            return True

    def subscriber_count(self, topic: str) -> int:
        with self._lock:
            return len(self._subs.get(topic, ()))

    def publish(self, topic: str, event: Any = None, *,
                raise_on_error: bool = False) -> PublishReport:
        """Deliver ``event`` serially to all subscribers of ``topic``.

        Subscriber exceptions are isolated: every remaining subscriber is
        still invoked, and every failure is returned in ``report.errors``.
        Handlers are invoked outside the internal lock and may (un)subscribe.
        """
        with self._lock:
            snapshot = list(self._subs.get(topic, ()))

        report = PublishReport(topic=topic, event=event)
        for order, sub in enumerate(snapshot):
            # Re-check liveness immediately before the call: an earlier
            # handler may have unsubscribed this one during this dispatch.
            if not sub.active:
                report.skipped_unsubscribed.append(sub.token)
                continue
            try:
                sub.handler(event)
            except Exception as exc:  # isolate ordinary errors only
                report.errors.append(SubscriberError(
                    topic=topic,
                    token=sub.token,
                    subscriber=sub.name,
                    error=exc,
                    order=order,
                ))
            else:
                report.delivered.append(sub.token)

        if raise_on_error and report.errors:
            raise AggregatePublishError(report)
        return report
