"""Simulated batch-submission server used by the self-tests and benchmark.

It models:
  * per-item scripted outcomes (success / retryable / non-retryable failure),
  * side effects (each successful apply is recorded in a ledger; a duplicated
    side effect would show up as the same item_id applied twice),
  * whole-batch transport failures (BatchTransportError) where part of the
    batch was already applied server-side, forcing client-side reconciliation,
  * a per-item ``query`` endpoint used for reconciliation.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List, Optional, Sequence

from batch_retry import (
    BatchClient,
    BatchTransportError,
    Item,
    ItemError,
    ItemResult,
    ItemStatus,
    classify_error,
    ErrorKind,
)


class SimulatedBatchService(BatchClient):
    """In-memory fake server.

    plan[item_id] is a list of outcomes consumed in order, one per submit call
    that carries the item. An outcome is:
      "OK"                  -> item applied successfully
      ItemError(...)        -> item rejected with that error (no side effect)
    When the plan for an item is exhausted, "OK" is assumed.

    transport_fail_on_calls: set of 1-based submit-call indices on which the
    server "loses the connection" after applying its scripted per-item work:
    the per-item effects happen, but the client only sees BatchTransportError.
    """

    def __init__(
        self,
        plan: Optional[Dict[str, List[object]]] = None,
        transport_fail_on_calls: Optional[set] = None,
    ) -> None:
        self.plan = plan or {}
        self.transport_fail_on_calls = transport_fail_on_calls or set()
        self.submit_calls = 0
        self.applied_ledger: List[str] = []          # one entry per side effect
        self.apply_count: Counter = Counter()        # item_id -> side effects
        self.last_error: Dict[str, ItemError] = {}   # item_id -> last failure
        self.submitted_item_ids: List[str] = []      # every item ever sent

    # -- server internals ----------------------------------------------------

    def _next_outcome(self, item_id: str) -> object:
        script = self.plan.get(item_id)
        if script:
            return script.pop(0)
        return "OK"

    def _apply(self, item_id: str) -> None:
        self.applied_ledger.append(item_id)
        self.apply_count[item_id] += 1

    # -- BatchClient interface ------------------------------------------------

    def submit(self, items: Sequence[Item]) -> Dict[str, ItemResult]:
        self.submit_calls += 1
        results: Dict[str, ItemResult] = {}
        for item in items:
            self.submitted_item_ids.append(item.item_id)
            if self.apply_count[item.item_id] > 0:
                # Server-side idempotency guard: a correctly behaving client
                # never resends a succeeded item, so this branch should be
                # unreachable in the tests. It exists to *detect* duplicates.
                raise AssertionError(
                    f"duplicate submission of already-applied item {item.item_id}"
                )
            outcome = self._next_outcome(item.item_id)
            if outcome == "OK":
                self._apply(item.item_id)
                results[item.item_id] = ItemResult(item.item_id, ItemStatus.SUCCESS)
            else:
                assert isinstance(outcome, ItemError)
                self.last_error[item.item_id] = outcome
                kind = classify_error(outcome.code)
                status = (
                    ItemStatus.FAILED_RETRYABLE
                    if kind is ErrorKind.RETRYABLE
                    else ItemStatus.FAILED_NON_RETRYABLE
                )
                results[item.item_id] = ItemResult(
                    item.item_id, status, last_error=outcome
                )
        if self.submit_calls in self.transport_fail_on_calls:
            raise BatchTransportError("simulated whole-batch timeout/reset")
        return results

    def query(self, item_id: str) -> ItemResult:
        if self.apply_count[item_id] > 0:
            return ItemResult(item_id, ItemStatus.SUCCESS)
        err = self.last_error.get(item_id)
        if err is not None and classify_error(err.code) is ErrorKind.NON_RETRYABLE:
            return ItemResult(item_id, ItemStatus.FAILED_NON_RETRYABLE, last_error=err)
        # Never applied and no permanent error: still pending -> retryable.
        return ItemResult(
            item_id,
            ItemStatus.FAILED_RETRYABLE,
            last_error=err or ItemError("TIMEOUT", "not applied (reconciled)"),
        )
