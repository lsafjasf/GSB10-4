"""Self-tests for ack_window (standard library only)."""

import random
import threading
import time
import unittest

from ack_window import AckWindow, AckWindowError, UnknownSequence, WindowFull


class AckWindowTests(unittest.TestCase):
    def test_in_order_ack(self):
        w = AckWindow(4)
        self.assertEqual([w.send() for _ in range(4)], [1, 2, 3, 4])
        with self.assertRaises(WindowFull):
            w.try_send()

        results = [w.ack(seq) for seq in (1, 2, 3, 4)]
        self.assertEqual([r.status for r in results], ["accepted"] * 4)
        self.assertEqual([r.base for r in results], [2, 3, 4, 5])
        self.assertEqual([r.released for r in results], [1, 1, 1, 1])
        self.assertEqual([r.unacked for r in results], [3, 2, 1, 0])
        self.assertEqual([r.window_used for r in results], [3, 2, 1, 0])
        stats = w.stats()
        self.assertEqual(stats["sent"], 4)
        self.assertEqual(stats["acks_accepted"], 4)
        self.assertEqual(stats["acks_duplicate"], 0)

    def test_out_of_order_ack(self):
        w = AckWindow(8)
        for _ in range(8):
            w.send()

        results = [w.ack(seq) for seq in (3, 1, 5, 2, 4, 8, 7, 6)]
        self.assertEqual([r.base for r in results], [1, 2, 2, 4, 6, 6, 6, 9])
        self.assertEqual([r.released for r in results], [0, 1, 0, 2, 2, 0, 0, 3])
        self.assertEqual([r.unacked for r in results], [7, 6, 5, 4, 3, 2, 1, 0])
        self.assertEqual([r.window_used for r in results], [8, 7, 7, 5, 3, 3, 3, 0])
        self.assertEqual(sum(r.released for r in results), 8)
        snap = w.snapshot()
        self.assertEqual((snap.base, snap.next_seq, snap.acked_ahead), (9, 9, 0))

    def test_duplicate_ack_is_idempotent(self):
        w = AckWindow(5)
        for _ in range(5):
            w.send()

        results = [w.ack(seq) for seq in (2, 2, 1, 2, 3, 3)]
        self.assertEqual(
            [r.status for r in results],
            ["accepted", "duplicate", "accepted", "duplicate", "accepted", "duplicate"],
        )
        # unacked must never be decremented twice for the same seq.
        self.assertEqual([r.unacked for r in results], [4, 4, 3, 3, 2, 2])
        self.assertEqual([r.window_used for r in results], [5, 5, 3, 3, 2, 2])
        self.assertEqual([r.released for r in results], [0, 0, 2, 0, 1, 0])
        stats = w.stats()
        self.assertEqual(stats["acks_accepted"], 3)
        self.assertEqual(stats["acks_duplicate"], 3)

    def test_full_window_pauses_and_long_silence_times_out(self):
        w = AckWindow(2)
        w.send()
        w.send()
        with self.assertRaises(WindowFull):
            w.try_send()

        started = time.monotonic()
        with self.assertRaises(WindowFull):
            w.send(timeout=0.05)
        self.assertGreaterEqual(time.monotonic() - started, 0.03)

        sent = []
        thread = threading.Thread(target=lambda: sent.append(w.send()))
        thread.start()
        time.sleep(0.05)
        self.assertTrue(thread.is_alive(), "publisher must stay paused while full")
        w.ack(1)
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(sent, [3])
        self.assertGreaterEqual(w.stats()["pauses"], 2)

    def test_close_wakes_blocked_sender(self):
        w = AckWindow(1)
        w.send()
        errors = []

        def blocked_send():
            try:
                w.send()
            except AckWindowError as exc:
                errors.append(exc)

        thread = threading.Thread(target=blocked_send)
        thread.start()
        time.sleep(0.02)
        w.close()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        with self.assertRaises(AckWindowError):
            w.send()

    def test_unknown_future_sequence_rejected(self):
        w = AckWindow(2)
        w.send()
        with self.assertRaises(UnknownSequence):
            w.ack(2)
        with self.assertRaises(TypeError):
            w.ack(1.5)

    def test_randomized_out_of_order_matches_in_order(self):
        rng = random.Random(20261005)
        total, capacity = 600, 17

        shuffled = AckWindow(capacity)
        in_order = AckWindow(capacity)
        oracle_acked = set()
        sent = 0
        shuffled_boundaries = []
        in_order_boundaries = []

        while sent < total:
            block = min(capacity, total - sent)
            block_seqs = [shuffled.send() for _ in range(block)]
            for _ in range(block):
                in_order.send()
            sent += block

            order = list(block_seqs)
            rng.shuffle(order)
            for seq in order:
                oracle_acked.add(seq)
                result = shuffled.ack(seq)
                self.assertEqual(result.unacked, sent - len(oracle_acked))
                self.assertEqual(result.window_used, shuffled.next_seq - shuffled.base)
            for seq in block_seqs:
                in_order.ack(seq)

            shuffled_boundaries.append(shuffled.snapshot())
            in_order_boundaries.append(in_order.snapshot())

        # At every block boundary (and finally) the out-of-order run reaches
        # exactly the same window state as the fully in-order run.
        self.assertEqual(shuffled_boundaries, in_order_boundaries)
        self.assertEqual(shuffled.stats()["acks_accepted"], total)
        self.assertEqual(in_order.stats()["acks_accepted"], total)
        final = shuffled.snapshot()
        self.assertEqual((final.base, final.unacked, final.window_used), (total + 1, 0, 0))

    def test_memory_bounded_by_capacity(self):
        w = AckWindow(512)
        for _ in range(512):
            w.send()
        mem_full = w.memory_bytes()
        self.assertGreater(mem_full, 0)
        self.assertEqual(w.stats()["max_window_used"], 512)

        for seq in range(2, 513):  # everything except base: max selective-ack set
            w.ack(seq)
        snap = w.snapshot()
        self.assertEqual((snap.unacked, snap.acked_ahead, snap.window_used), (1, 511, 512))
        self.assertLessEqual(snap.window_used, 512)
        self.assertGreater(w.memory_bytes(), 0)

        w.ack(1)
        snap = w.snapshot()
        self.assertEqual((snap.unacked, snap.acked_ahead, snap.window_used), (0, 0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
