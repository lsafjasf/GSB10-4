"""Self-tests for leakdet: covers full release, partial leak, exception-path
leak, double release, refcounting, abandonment, accounting and false-positive
exclusion. Run: python3 -m unittest -v test_leakdet
"""

import gc
import time
import unittest

from leakdet import LeakTracker


def make_connection(tracker, label=None):
    """Simulates acquiring a DB connection."""
    return tracker.acquire(kind="connection", label=label)


def make_fd(tracker, label=None):
    """Simulates acquiring a file handle."""
    return tracker.acquire(kind="fd", label=label)


class TestFullRelease(unittest.TestCase):
    def test_all_released_no_leaks(self):
        t = LeakTracker()
        for _ in range(100):
            make_connection(t).release()
        report = t.check_leaks()
        self.assertEqual(report.leaks, [])
        acc = report.accounting
        self.assertEqual(acc["acquired"], 100)
        self.assertEqual(acc["released"], 100)
        self.assertEqual(acc["live"], 0)
        self.assertTrue(t.accounting()["balanced"])

    def test_release_is_logged_with_site(self):
        t = LeakTracker()
        make_connection(t, label="db-conn").release(note="normal close")
        log = t.release_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["site"], "db-conn")
        self.assertEqual(log[0]["note"], "normal close")
        self.assertGreaterEqual(log[0]["held_for"], 0.0)


class TestPartialLeak(unittest.TestCase):
    def test_leak_attributed_to_acquire_site(self):
        t = LeakTracker()
        for _ in range(3):
            make_connection(t).release()          # properly closed
        make_fd(t, label="fd:open_config")        # leaked: never released
        make_fd(t, label="fd:open_config")        # leaked again, same site
        make_connection(t, label="db:query")      # leaked, different site

        report = t.check_leaks()
        self.assertEqual(len(report.leaks), 3)
        groups = report.by_site()
        self.assertEqual(len(groups["fd:open_config"]), 2)
        self.assertEqual(len(groups["db:query"]), 1)
        # accounting: 6 acquired, 3 released, 3 live == 3 leaks
        acc = report.accounting
        self.assertEqual(acc["acquired"] - acc["released"], 3)
        self.assertEqual(acc["live"], len(report.leaks))

    def test_leak_carries_acquire_stack(self):
        t = LeakTracker()
        make_connection(t)  # no label -> attributed by file:line of this call
        report = t.check_leaks()
        self.assertEqual(len(report.leaks), 1)
        stack = report.leaks[0].acquire_stack
        self.assertIn("test_leak_carries_acquire_stack", stack)
        self.assertIn(__file__, stack)


class TestExceptionPath(unittest.TestCase):
    def test_release_in_finally_is_recorded(self):
        t = LeakTracker()
        try:
            h = make_connection(t, label="db:tx")
            raise RuntimeError("boom")
        except RuntimeError:
            pass
        finally:
            h.release(note="finally-cleanup")
        report = t.check_leaks()
        self.assertEqual(report.leaks, [])
        self.assertEqual(t.release_log()[0]["note"], "finally-cleanup")

    def test_context_manager_releases_on_exception(self):
        t = LeakTracker()
        with self.assertRaises(ValueError):
            with make_connection(t, label="db:ctx"):
                raise ValueError("query failed")
        report = t.check_leaks()
        self.assertEqual(report.leaks, [])
        self.assertEqual(t.release_log()[0]["note"], "ctx-exit:ValueError")

    def test_exception_skipping_release_is_detected(self):
        t = LeakTracker()
        def buggy():
            h = make_connection(t, label="db:buggy")
            raise RuntimeError("crashed before release")  # h never released
        with self.assertRaises(RuntimeError):
            buggy()
        report = t.check_leaks()
        self.assertEqual(len(report.leaks), 1)
        self.assertEqual(report.leaks[0].site, "db:buggy")


class TestDoubleRelease(unittest.TestCase):
    def test_double_release_recorded_not_counted(self):
        t = LeakTracker()
        h = make_connection(t)
        self.assertTrue(h.release())    # first release frees it
        self.assertFalse(h.release())   # second release -> double-release event
        report = t.check_leaks()
        self.assertEqual(report.leaks, [])
        self.assertEqual(len(report.double_releases), 1)
        self.assertEqual(report.double_releases[0]["hid"], h.hid)
        # accounting stays consistent: double release does not go negative
        acc = report.accounting
        self.assertEqual(acc["acquired"], 1)
        self.assertEqual(acc["released"], 1)
        self.assertEqual(acc["live"], 0)


class TestRefcount(unittest.TestCase):
    def test_retain_requires_matching_release(self):
        t = LeakTracker()
        h = make_connection(t, label="shared-conn")
        h.retain()                       # refcount 2: two logical owners
        self.assertFalse(h.release())    # refcount 1 -> still live, not freed
        self.assertEqual(len(t.check_leaks().leaks), 1)
        self.assertTrue(h.release())     # refcount 0 -> freed
        self.assertEqual(t.check_leaks().leaks, [])

    def test_retain_on_dead_handle_raises(self):
        t = LeakTracker()
        h = make_connection(t)
        h.release()
        with self.assertRaises(ValueError):
            h.retain()


class TestAbandonedHandle(unittest.TestCase):
    def test_gc_without_release_is_definite_leak(self):
        t = LeakTracker()
        make_connection(t, label="db:dropped")  # handle object dropped at once
        gc.collect()
        report = t.check_leaks()
        self.assertEqual(len(report.abandoned), 1)
        self.assertEqual(report.abandoned[0].site, "db:dropped")


class TestJudgementBasis(unittest.TestCase):
    def test_age_threshold_filters_young_handles(self):
        t = LeakTracker()
        old = make_connection(t, label="old")
        time.sleep(0.02)
        young = make_connection(t, label="young")
        report = t.check_leaks(max_age=0.01)
        self.assertEqual([h.label for h in report.leaks], ["old"])
        old.release()
        young.release()

    def test_exclude_predicate_removes_false_positives(self):
        t = LeakTracker()
        make_connection(t, label="pool:long-lived")   # legitimately long-lived
        make_connection(t, label="db:real-leak")
        report = t.check_leaks(exclude=lambda h: h.label.startswith("pool:"))
        self.assertEqual([h.label for h in report.leaks], ["db:real-leak"])


class TestAccounting(unittest.TestCase):
    def test_acquired_minus_released_equals_leaks(self):
        t = LeakTracker()
        handles = [make_connection(t) for _ in range(50)]
        for h in handles[:37]:
            h.release()
        handles[40].release()  # double release attempt on... no, 40 is live
        report = t.check_leaks()
        acc = report.accounting
        self.assertEqual(acc["acquired"], 50)
        self.assertEqual(acc["released"], 38)
        self.assertEqual(acc["acquired"] - acc["released"], len(report.leaks))
        self.assertEqual(len(report.leaks), 12)


if __name__ == "__main__":
    unittest.main()
