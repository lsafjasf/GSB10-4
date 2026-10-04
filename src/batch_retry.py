"""Per-item retry for batch submission APIs (Python 3 standard library only).

Guarantees
----------
1. Only items whose *last* result is a *retryable* failure are resubmitted.
   Successful items are never sent again, so their side effects cannot repeat.
2. Retryable and non-retryable failures are classified (see ``classify_error``).
3. Every item ends in a *decided* state: SUCCESS, FAILED_RETRYABLE (retry
   budget exhausted) or FAILED_NON_RETRYABLE. If a retry call itself fails in a
   way that leaves per-item outcomes ambiguous (whole-batch timeout /
   connection loss), a per-item reconciliation query decides each item; no
   item is ever left UNKNOWN.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional, Sequence


class ErrorKind(Enum):
    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"


class ItemStatus(Enum):
    SUCCESS = "SUCCESS"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_NON_RETRYABLE = "FAILED_NON_RETRYABLE"


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------
#
# Classification rule: a failure code is retryable iff the same request may
# succeed when sent again *unchanged*. Transport/transient server failures are
# retryable; request-content/client-permission failures are not, because the
# failure is caused by the item itself and repeating it cannot help.
#
# Retryable samples (server/transport, request body is fine):
#   TIMEOUT           request exceeded deadline; may have applied, unknown here
#   CONNECTION_RESET  TCP connection dropped / reset mid-flight
#   RATE_LIMIT        HTTP 429, throttled, retry after backoff
#   SERVER_5XX        HTTP 500/502/503/504, server-side transient fault
#   UNAVAILABLE       service in temporary maintenance / circuit open
#
# Non-retryable samples (the item itself is invalid; identical retry is futile):
#   VALIDATION        HTTP 400/422, schema or business-rule validation error
#   AUTH              HTTP 401/403, bad credentials / forbidden
#   NOT_FOUND         HTTP 404, referenced resource does not exist
#   CONFLICT          HTTP 409, version conflict / precondition failed
#   DUPLICATE         idempotency key already exists with different payload
#   PAYLOAD_TOO_LARGE HTTP 413, item over size limit

RETRYABLE_CODES = frozenset({
    "TIMEOUT", "CONNECTION_RESET", "RATE_LIMIT", "SERVER_5XX", "UNAVAILABLE",
})

NON_RETRYABLE_CODES = frozenset({
    "VALIDATION", "AUTH", "NOT_FOUND", "CONFLICT", "DUPLICATE",
    "PAYLOAD_TOO_LARGE",
})


@dataclass(frozen=True)
class ItemError(Exception):
    code: str
    message: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}" if self.message else self.code


class BatchTransportError(Exception):
    """Whole-batch call failed so per-item outcomes are ambiguous.

    Example: the HTTP request timed out. Some items may have been applied
    server-side, so the client must reconcile per item rather than blindly
    retry the whole batch.
    """


def classify_error(code: str) -> ErrorKind:
    if code in RETRYABLE_CODES:
        return ErrorKind.RETRYABLE
    if code in NON_RETRYABLE_CODES:
        return ErrorKind.NON_RETRYABLE
    # Unknown codes are treated as non-retryable for safety: an unrecognised
    # failure must not cause unbounded identical retries.
    return ErrorKind.NON_RETRYABLE


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Item:
    item_id: str
    payload: object


@dataclass
class ItemResult:
    item_id: str
    status: ItemStatus
    attempts: int = 0                      # submit calls that carried this item
    last_error: Optional[ItemError] = None
    decided_by: str = "submit"             # "submit" or "reconcile"


@dataclass
class BatchOutcome:
    table: Dict[str, ItemResult]
    rounds: int                            # number of submit calls made
    retried_items: Counter = field(default_factory=Counter)  # item_id -> retries


# ---------------------------------------------------------------------------
# Retry driver
# ---------------------------------------------------------------------------

class BatchClient:
    """Interface a real adapter must implement."""

    def submit(self, items: Sequence[Item]) -> Dict[str, ItemResult]:
        """Submit one batch. Raises BatchTransportError if per-item outcomes
        cannot be determined from this call alone."""
        raise NotImplementedError

    def query(self, item_id: str) -> ItemResult:
        """Reconciliation lookup: the server reports the true current state of
        one item (used after an ambiguous transport failure)."""
        raise NotImplementedError


def _submit_once_reconciled(
    client: BatchClient,
    items: Sequence[Item],
    attempts_delta: int,
) -> Dict[str, ItemResult]:
    """One submit round, fully decided.

    On an ambiguous transport failure, reconcile every item individually so
    the returned table never contains an UNKNOWN entry.
    """
    try:
        results = client.submit(items)
    except BatchTransportError:
        results = {}
        for item in items:
            r = client.query(item.item_id)
            r.decided_by = "reconcile"
            r.attempts = attempts_delta
            results[item.item_id] = r

    missing = {it.item_id for it in items} - results.keys()
    if missing:
        # A well-behaved adapter answers for every item; if it doesn't, query.
        for item_id in missing:
            r = client.query(item_id)
            r.decided_by = "reconcile"
            results[item_id] = r
    return results


def submit_batch_with_retry(
    client: BatchClient,
    items: Sequence[Item],
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    backoff: Callable[[int], float] = lambda round_index: 0.0,
) -> BatchOutcome:
    """Submit ``items`` and retry *only* the still-failed retryable items.

    Each round submits a batch containing precisely the items whose current
    status is FAILED_RETRYABLE; SUCCESS and FAILED_NON_RETRYABLE items are
    never sent again.
    """
    table: Dict[str, ItemResult] = {}
    retried_items: Counter = Counter()
    rounds = 0

    first = _submit_once_reconciled(client, items, attempts_delta=1)
    rounds += 1
    for it in items:
        r = first[it.item_id]
        r.attempts = 1
        table[it.item_id] = r

    for round_index in range(1, max_retries + 1):
        pending = [
            it for it in items
            if table[it.item_id].status == ItemStatus.FAILED_RETRYABLE
        ]
        if not pending:
            break

        delay = backoff(round_index)
        if delay > 0:
            sleep(delay)

        round_results = _submit_once_reconciled(client, pending, attempts_delta=1)
        rounds += 1

        for it in pending:
            previous = table[it.item_id]
            r = round_results[it.item_id]
            retried_items[it.item_id] += 1
            # Preserve the retryable error if reconciliation says "not applied":
            # the item is carried forward unchanged with attempts incremented.
            r.attempts = previous.attempts + 1
            table[it.item_id] = r

    return BatchOutcome(table=table, rounds=rounds, retried_items=retried_items)


# ---------------------------------------------------------------------------
# Reference implementation: every item submitted independently, no batching.
# Used for differential testing （对拍） against the batch + retry path.
# ---------------------------------------------------------------------------

def submit_each_independently(
    client: BatchClient,
    items: Sequence[Item],
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    backoff: Callable[[int], float] = lambda round_index: 0.0,
) -> BatchOutcome:
    table: Dict[str, ItemResult] = {}
    retried_items: Counter = Counter()
    rounds = 0
    for item in items:
        outcome = submit_batch_with_retry(
            client, [item], max_retries=max_retries, sleep=sleep, backoff=backoff
        )
        table[item.item_id] = outcome.table[item.item_id]
        rounds += outcome.rounds
        retried_items.update(outcome.retried_items)
    return BatchOutcome(table=table, rounds=rounds, retried_items=retried_items)
