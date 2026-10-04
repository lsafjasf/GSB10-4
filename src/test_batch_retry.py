"""Self-tests for the per-item batch retry library (stdlib unittest only).

Run:  python3 test_batch_retry.py -v
"""

import random
import unittest

from batch_retry import (
    ErrorKind,
    Item,
    ItemError,
    ItemStatus,
    NON_RETRYABLE_CODES,
    RETRYABLE_CODES,
    classify_error,
    submit_batch_with_retry,
    submit_each_independently,
)
from simulated_service import SimulatedBatchService


def make_items(n, prefix="it"):
    return [Item(f"{prefix}-{i:04d}", payload=i) for i in range(n)]


def statuses(outcome):
    return {iid: r.status for iid, r in outcome.table.items()}


class TestClassification(unittest.TestCase):
    def test_retryable_samples(self):
        samples = {
            "TIMEOUT": "deadline exceeded, may have applied",
            "CONNECTION_RESET": "tcp reset by peer",
            "RATE_LIMIT": "http 429 too many requests",
            "SERVER_5XX": "http 503 service unavailable",
            "UNAVAILABLE": "circuit breaker open",
        }
        for code in samples:
            self.assertIs(classify_error(code), ErrorKind.RETRYABLE, code)
        self.assertEqual(set(samples), RETRYABLE_CODES)

    def test_non_retryable_samples(self):
        samples = {
            "VALIDATION": "http 422 schema error",
            "AUTH": "http 403 forbidden",
            "NOT_FOUND": "http 404 missing resource",
            "CONFLICT": "http 409 version conflict",
            "DUPLICATE": "idempotency key reused with different payload",
            "PAYLOAD_TOO_LARGE": "http 413",
        }
        for code in samples:
            self.assertIs(classify_error(code), ErrorKind.NON_RETRYABLE, code)
        self.assertEqual(set(samples), NON_RETRYABLE_CODES)

    def test_unknown_code_is_safe(self):
        self.assertIs(classify_error("SOMETHING_NEW"), ErrorKind.NON_RETRYABLE)


class TestScenarios(unittest.TestCase):
    def test_all_success(self):
        items = make_items(50)
        svc = SimulatedBatchService()
        out = submit_batch_with_retry(svc, items)
        self.assertTrue(all(s is ItemStatus.SUCCESS for s in statuses(out).values()))
        self.assertEqual(out.rounds, 1)
        self.assertEqual(len(svc.applied_ledger), 50)
        self.assertEqual(len(set(svc.applied_ledger)), 50, "no duplicated side effects")

    def test_all_fail_non_retryable(self):
        items = make_items(30)
        plan = {it.item_id: [ItemError("VALIDATION", "bad field")] for it in items}
        svc = SimulatedBatchService(plan)
        out = submit_batch_with_retry(svc, items, max_retries=3)
        self.assertTrue(
            all(s is ItemStatus.FAILED_NON_RETRYABLE for s in statuses(out).values())
        )
        self.assertEqual(out.rounds, 1, "non-retryable failures must not be retried")
        self.assertEqual(svc.applied_ledger, [])

    def test_all_fail_retryable_exhausts_budget(self):
        items = make_items(20)
        plan = {it.item_id: [ItemError("SERVER_5XX")] * 10 for it in items}
        svc = SimulatedBatchService(plan)
        out = submit_batch_with_retry(svc, items, max_retries=3)
        self.assertTrue(
            all(s is ItemStatus.FAILED_RETRYABLE for s in statuses(out).values())
        )
        self.assertEqual(out.rounds, 4)  # 1 initial + 3 retries
        for r in out.table.values():
            self.assertEqual(r.attempts, 4)
        self.assertEqual(svc.applied_ledger, [])

    def test_partial_failure_only_failures_retried(self):
        items = make_items(10)
        # items 0,1 succeed immediately; 2,3 fail retryable once then succeed;
        # 4 fails non-retryable; 5 keeps failing retryable.
        plan = {
            "it-0002": [ItemError("RATE_LIMIT"), "OK"],
            "it-0003": [ItemError("TIMEOUT"), "OK"],
            "it-0004": [ItemError("CONFLICT")],
            "it-0005": [ItemError("SERVER_5XX")] * 10,
        }
        svc = SimulatedBatchService(plan)
        out = submit_batch_with_retry(svc, items, max_retries=3)

        st = statuses(out)
        for i in (0, 1, 2, 3, 6, 7, 8, 9):
            self.assertIs(st[f"it-{i:04d}"], ItemStatus.SUCCESS, i)
        self.assertIs(st["it-0004"], ItemStatus.FAILED_NON_RETRYABLE)
        self.assertIs(st["it-0005"], ItemStatus.FAILED_RETRYABLE)

        # Successful-on-first-try items were submitted exactly once.
        for i in (0, 1, 6, 7, 8, 9):
            self.assertEqual(out.table[f"it-{i:04d}"].attempts, 1)
        self.assertEqual(out.table["it-0002"].attempts, 2)
        self.assertEqual(out.table["it-0003"].attempts, 2)
        self.assertEqual(out.table["it-0004"].attempts, 1, "non-retryable not retried")
        self.assertEqual(out.table["it-0005"].attempts, 4)

        # Every side effect happened exactly once.
        self.assertEqual(sorted(svc.applied_ledger), sorted(set(svc.applied_ledger)))
        self.assertEqual(len(svc.applied_ledger), 8)
        self.assertEqual(svc.apply_count["it-0002"], 1)
        self.assertEqual(svc.apply_count["it-0003"], 1)

    def test_retry_fails_again_then_eventually_succeeds(self):
        items = make_items(3)
        plan = {"it-0001": [ItemError("SERVER_5XX"), ItemError("RATE_LIMIT"), "OK"]}
        svc = SimulatedBatchService(plan)
        out = submit_batch_with_retry(svc, items, max_retries=5)
        self.assertIs(out.table["it-0001"].status, ItemStatus.SUCCESS)
        self.assertEqual(out.table["it-0001"].attempts, 3)
        self.assertEqual(out.retried_items["it-0001"], 2)
        self.assertEqual(svc.apply_count["it-0001"], 1)

    def test_retry_fails_again_and_stays_failed_decided(self):
        items = make_items(2)
        plan = {it.item_id: [ItemError("UNAVAILABLE")] * 100 for it in items}
        svc = SimulatedBatchService(plan)
        out = submit_batch_with_retry(svc, items, max_retries=2)
        for r in out.table.values():
            self.assertIs(r.status, ItemStatus.FAILED_RETRYABLE)
            self.assertEqual(r.attempts, 3)
            self.assertIsNotNone(r.last_error)

    def test_batch_transport_failure_is_reconciled_no_unknown(self):
        items = make_items(6)
        # Round 1: whole batch "times out" server-side after applying its work.
        # it-0000/0001 applied OK before the timeout; it-0002 got a retryable
        # error; it-0003 got a non-retryable error; 0004/0005 got retryable
        # errors and will succeed on the retry round.
        plan = {
            "it-0002": [ItemError("RATE_LIMIT"), "OK"],
            "it-0003": [ItemError("VALIDATION")],
            "it-0004": [ItemError("SERVER_5XX"), "OK"],
            "it-0005": [ItemError("TIMEOUT"), "OK"],
        }
        svc = SimulatedBatchService(plan, transport_fail_on_calls={1})
        out = submit_batch_with_retry(svc, items, max_retries=3)

        st = statuses(out)
        for i in (0, 1, 2, 4, 5):
            self.assertIs(st[f"it-{i:04d}"], ItemStatus.SUCCESS, i)
        self.assertIs(st["it-0003"], ItemStatus.FAILED_NON_RETRYABLE)
        # No item may be left undecided: every status is one of the 3 finals.
        self.assertEqual(set(st.values()) <= set(ItemStatus), True)
        # Reconciliation was used on round 1.
        self.assertEqual(out.table["it-0000"].decided_by, "reconcile")
        # Items applied before the timeout must NOT be applied again.
        self.assertEqual(svc.apply_count["it-0000"], 1)
        self.assertEqual(svc.apply_count["it-0001"], 1)
        self.assertEqual(len(svc.applied_ledger), len(set(svc.applied_ledger)))

    def test_no_duplicate_side_effects_under_mixed_chaos(self):
        rng = random.Random(20261005)
        items = make_items(500)
        plan = {}
        for it in items:
            script = []
            for _ in range(rng.randint(0, 4)):
                script.append(ItemError(rng.choice(sorted(RETRYABLE_CODES))))
            if rng.random() < 0.1:
                script.append(ItemError(rng.choice(sorted(NON_RETRYABLE_CODES))))
            plan[it.item_id] = script
        svc = SimulatedBatchService(plan)
        out = submit_batch_with_retry(svc, items, max_retries=6)
        self.assertEqual(len(svc.applied_ledger), len(set(svc.applied_ledger)))
        for iid, count in svc.apply_count.items():
            self.assertEqual(count, 1, iid)
        for r in out.table.values():
            self.assertIn(r.status, set(ItemStatus))


class TestDifferential(unittest.TestCase):
    """对拍： batch+retry must equal item-by-item submission."""

    def _random_plan(self, seed, n):
        rng = random.Random(seed)
        plan = {}
        for i in range(n):
            script = []
            while rng.random() < 0.35:
                script.append(ItemError(rng.choice(sorted(RETRYABLE_CODES))))
            if rng.random() < 0.15:
                script.append(ItemError(rng.choice(sorted(NON_RETRYABLE_CODES))))
            plan[f"it-{i:04d}"] = script
        return plan

    def test_batch_matches_individual(self):
        for seed in (1, 7, 42, 2026):
            n = 300
            items = make_items(n)

            svc_batch = SimulatedBatchService(self._random_plan(seed, n))
            out_batch = submit_batch_with_retry(svc_batch, items, max_retries=8)

            svc_each = SimulatedBatchService(self._random_plan(seed, n))
            out_each = submit_each_independently(svc_each, items, max_retries=8)

            self.assertEqual(statuses(out_batch), statuses(out_each), f"seed={seed}")
            self.assertEqual(
                {k: v.attempts for k, v in out_batch.table.items()},
                {k: v.attempts for k, v in out_each.table.items()},
                f"seed={seed}",
            )
            # Same side effects, same multiplicity, same order-insensitive set.
            self.assertEqual(
                sorted(svc_batch.applied_ledger), sorted(svc_each.applied_ledger)
            )
            self.assertEqual(
                len(svc_batch.applied_ledger), len(set(svc_batch.applied_ledger))
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
