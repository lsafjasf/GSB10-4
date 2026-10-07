"""Self tests for the dynamic table library (standard-library unittest)."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dyntable import (  # noqa: E402
    BlockedHeaderBlock,
    CapacityLimitExceeded,
    Decoder,
    DynamicTable,
    Encoder,
    EvictedEntryReference,
    IndexOutOfRange,
    InvalidInstruction,
    LostUpdateError,
    OutOfOrderUpdateError,
    TableInvariantViolation,
    entry_size,
)
from dyntable.codec import F_INDEXED_PRE_BASE  # noqa: E402
from dyntable.wire import enc_str, enc_u8, enc_u32  # noqa: E402


class TestTableBasics(unittest.TestCase):
    def test_empty_table(self):
        table = DynamicTable(256)
        self.assertEqual(len(table), 0)
        self.assertEqual(table.size, 0)
        self.assertEqual(table.insert_count, 0)
        self.assertEqual(table.dropped_count, 0)
        self.assertIsNone(table.find("name", "value"))
        with self.assertRaises(IndexOutOfRange):
            table.get_absolute(0)
        with self.assertRaises(IndexOutOfRange):
            table.get_relative(0, base=0)
        table.check_invariant()

    def test_entry_size_accounting(self):
        table = DynamicTable(4096)
        self.assertTrue(table.insert("name", "value"))
        self.assertEqual(table.size, entry_size("name", "value"))
        self.assertEqual(table.size, 4 + 5 + 32)
        self.assertEqual(table.insert_count, 1)
        self.assertEqual(table.get_absolute(0), ("name", "value"))
        self.assertEqual(table.get_relative(0, base=1), ("name", "value"))

    def test_insert_goes_to_front_newest_first(self):
        table = DynamicTable(4096)
        for i in range(3):
            table.insert("n", str(i))
        snapshot = table.snapshot()
        self.assertEqual(
            [(n, v) for _abs, n, v in snapshot["entries"]],
            [("n", "2"), ("n", "1"), ("n", "0")],
        )
        self.assertEqual(
            [abs_idx for abs_idx, _n, _v in snapshot["entries"]],
            [2, 1, 0],
        )


class TestFullTableAndEviction(unittest.TestCase):
    def test_full_table_evicts_oldest_first(self):
        table = DynamicTable(128)
        # Each entry: 1 + 1 + 32 = 34 bytes -> at most 3 entries.
        for i in range(3):
            self.assertTrue(table.insert("n", str(i)))
        self.assertEqual(table.size, 102)
        self.assertEqual(len(table), 3)
        self.assertTrue(table.insert("n", "3"))
        self.assertEqual(table.dropped_count, 1)
        self.assertEqual(table.insert_count, 4)
        self.assertEqual(len(table), 3)
        with self.assertRaises(EvictedEntryReference):
            table.get_absolute(0)
        self.assertEqual(table.get_absolute(1), ("n", "1"))
        self.assertEqual(table.get_absolute(3), ("n", "3"))

    def test_capacity_shrink_triggers_eviction(self):
        table = DynamicTable(256)
        for i in range(6):
            table.insert("n", str(i))
        self.assertEqual(len(table), 6)
        table.set_capacity(34)
        self.assertEqual(len(table), 1)
        self.assertEqual(table.get_absolute(5), ("n", "5"))
        self.assertLessEqual(table.size, table.capacity)

    def test_oversized_entry_empties_table_not_inserted(self):
        table = DynamicTable(64)
        table.insert("n", "1")
        ok = table.insert("x" * 20, "y" * 20)
        self.assertFalse(ok)
        self.assertEqual(len(table), 0)
        self.assertEqual(table.size, 0)
        self.assertEqual(table.insert_count, 1)
        self.assertEqual(table.dropped_count, 1)
        table.check_invariant()

    def test_capacity_update_above_limit_rejected(self):
        table = DynamicTable(128)
        with self.assertRaises(CapacityLimitExceeded):
            table.set_capacity(129)
        with self.assertRaises(CapacityLimitExceeded):
            table.set_capacity(-1)
        # Table untouched.
        self.assertEqual(table.capacity, 128)

    def test_overflow_invariant_detection(self):
        table = DynamicTable(128)
        table.insert("n", "1")
        table.size = table.capacity + 1  # corrupt accounting on purpose
        with self.assertRaises(TableInvariantViolation):
            table.check_invariant()


class TestContinuousInsertionSync(unittest.TestCase):
    def test_continuous_insertion_keeps_ends_equal(self):
        enc = Encoder(256)
        dec = Decoder(256)
        rng = random.Random(42)
        for i in range(500):
            name = rng.choice(["a", "bb", "ccc", "dddd"])
            value = str(rng.randrange(20))
            enc.insert(name, value)
            if i % 7 == 0:
                dec.feed_stream_chunk(enc.flush_stream_chunk())
                self.assertEqual(
                    enc.table.snapshot(), dec.table.snapshot()
                )
        dec.feed_stream_chunk(enc.flush_stream_chunk())
        self.assertEqual(enc.table.snapshot(), dec.table.snapshot())
        self.assertGreater(enc.table.dropped_count, 0)

    def test_step_by_step_mixed_operations(self):
        enc = Encoder(200)
        dec = Decoder(200)

        def sync(op_desc):
            dec.feed_stream_chunk(enc.flush_stream_chunk())
            self.assertEqual(
                enc.table.snapshot(),
                dec.table.snapshot(),
                "state mismatch after: %s" % op_desc,
            )

        sync("initial empty")
        enc.set_capacity(140)
        sync("set_capacity(140)")
        for i in range(8):
            enc.insert("h", str(i))
            sync("insert h=%d" % i)
        enc.set_capacity(70)
        sync("set_capacity(70) shrink")
        for i in range(10):
            enc.insert("k", str(i))
            sync("insert k=%d" % i)
        enc.set_capacity(200)
        sync("set_capacity(200) grow")


class TestHeaderRoundTrip(unittest.TestCase):
    def test_literal_block_on_empty_table(self):
        enc, dec = Encoder(256), Decoder(256)
        headers = [(":status", "200"), ("x-empty", "")]
        block = enc.encode_header_block(headers, index=False)
        self.assertEqual(dec.decode_header_block(block), headers)
        self.assertEqual(enc.table.snapshot(), dec.table.snapshot())

    def test_indexed_and_name_ref_round_trip(self):
        enc, dec = Encoder(512), Decoder(512)
        first = [(":status", "200"), ("content-type", "text/html"),
                 ("cache-control", "max-age=0")]
        block1 = enc.encode_header_block(first)
        dec.feed_stream_chunk(enc.flush_stream_chunk())
        self.assertEqual(dec.decode_header_block(block1), first)

        second = [(":status", "200"), ("content-type", "application/json"),
                  ("x-new", "yes")]
        block2 = enc.encode_header_block(second)
        dec.feed_stream_chunk(enc.flush_stream_chunk())
        self.assertEqual(dec.decode_header_block(block2), second)
        self.assertEqual(enc.table.snapshot(), dec.table.snapshot())

    def test_ric_wraparound_across_many_inserts(self):
        enc, dec = Encoder(128), Decoder(128)  # max_entries=4, range 8
        for i in range(60):
            headers = [("n", str(i))]
            block = enc.encode_header_block(headers)
            dec.feed_stream_chunk(enc.flush_stream_chunk())
            self.assertEqual(dec.decode_header_block(block), headers)
        self.assertEqual(enc.table.snapshot(), dec.table.snapshot())


class TestErrorDetection(unittest.TestCase):
    def _deliver(self, enc, dec):
        dec.feed_stream_chunk(enc.flush_stream_chunk())

    def test_capacity_above_limit_instruction_rejected(self):
        import struct

        enc, dec = Encoder(128), Decoder(128)
        with self.assertRaises(CapacityLimitExceeded):
            enc.set_capacity(129)
        # Forge an illegal instruction chunk (seq=0, set_capacity 5000).
        payload = enc_u8(0x02) + enc_u32(5000)
        forged = struct.pack(">II", 0, len(payload)) + payload
        with self.assertRaises(CapacityLimitExceeded):
            dec.feed_stream_chunk(forged)

    def test_index_out_of_range_is_reported(self):
        enc, dec = Encoder(256), Decoder(256)
        # Prefix: EIC=0, sign=0, delta_base=0; field: indexed rel 99.
        bad = enc_u32(0) + enc_u8(0) + enc_u32(0)
        bad += enc_u8(F_INDEXED_PRE_BASE) + enc_u32(99)
        with self.assertRaises(IndexOutOfRange):
            dec.decode_header_block(bad)

    def test_evicted_entry_reference_is_reported(self):
        enc, dec = Encoder(160), Decoder(160)  # fits 4 x 34-byte entries
        block = enc.encode_header_block(
            [("n", "0"), ("n", "1"), ("n", "2"), ("n", "3")]
        )
        self._deliver(enc, dec)
        self.assertEqual(
            dec.decode_header_block(block),
            [("n", "0"), ("n", "1"), ("n", "2"), ("n", "3")],
        )
        # One more insert evicts abs 0 (5 x 34 = 170 > 160).
        block2 = enc.encode_header_block([("n", "4")])
        self._deliver(enc, dec)
        self.assertEqual(dec.decode_header_block(block2), [("n", "4")])
        self.assertEqual(dec.table.dropped_count, 1)
        # Forge a block with base=5 referencing rel 4 -> abs 0 (evicted).
        bad = (enc_u32(0) + enc_u8(0) + enc_u32(5)
               + enc_u8(F_INDEXED_PRE_BASE) + enc_u32(4))
        with self.assertRaises(EvictedEntryReference):
            dec.decode_header_block(bad)

    def test_lost_update_chunk_is_detected(self):
        enc, dec = Encoder(256), Decoder(256)
        enc.insert("a", "1")
        lost = enc.flush_stream_chunk()  # seq 0, never delivered
        enc.insert("b", "2")
        second = enc.flush_stream_chunk()  # seq 1
        with self.assertRaises(LostUpdateError) as ctx:
            dec.feed_stream_chunk(second)
        self.assertIn("lost", str(ctx.exception))
        # Feeding the missing chunk afterwards restores order.
        dec.feed_stream_chunk(lost)
        dec.feed_stream_chunk(second)
        self.assertEqual(enc.table.snapshot(), dec.table.snapshot())

    def test_out_of_order_chunk_is_detected(self):
        enc, dec = Encoder(256), Decoder(256)
        enc.insert("a", "1")
        first = enc.flush_stream_chunk()
        enc.insert("b", "2")
        second = enc.flush_stream_chunk()
        dec.feed_stream_chunk(first)
        with self.assertRaises(OutOfOrderUpdateError):
            dec.feed_stream_chunk(first)  # replay/duplicate
        dec.feed_stream_chunk(second)
        self.assertEqual(enc.table.snapshot(), dec.table.snapshot())

    def test_block_before_updates_is_blocked(self):
        enc, dec = Encoder(256), Decoder(256)
        block = enc.encode_header_block([("a", "1")])
        # Chunk is not delivered; block arrives first.
        with self.assertRaises(BlockedHeaderBlock):
            dec.decode_header_block(block)
        # Delivering the update then retries decode successfully.
        dec.feed_stream_chunk(enc.flush_stream_chunk())
        self.assertEqual(dec.decode_header_block(block), [("a", "1")])

    def test_oversized_insert_instruction_is_rejected(self):
        import struct

        enc, dec = Encoder(64), Decoder(64)
        # Bypass the encoder API and forge an oversized insert instruction.
        payload = enc_u8(0x01) + enc_str("x" * 20) + enc_str("y" * 20)
        forged = struct.pack(">II", 0, len(payload)) + payload
        with self.assertRaises(InvalidInstruction):
            dec.feed_stream_chunk(forged)


if __name__ == "__main__":
    unittest.main(verbosity=2)
