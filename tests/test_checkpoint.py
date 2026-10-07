import json
import os
import tempfile
import unittest

from sqlchunk.checkpoint import (
    CheckpointCorrupted,
    CheckpointStore,
    STATUS_DONE,
)
from sqlchunk.chunker import split_and_chunk

SQL = b"CREATE TABLE t(a INT); INSERT INTO t VALUES(1); INSERT INTO t VALUES(2);"


class CheckpointTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "run.ckpt")
        self.chunks = split_and_chunk(SQL, 40)

    def open_or_create(self):
        return CheckpointStore.open_or_create(self.path, SQL, self.chunks, 40)

    def test_create_and_resume(self):
        store = self.open_or_create()
        self.assertEqual(store.next_pending(), 0)
        store.mark_started(0)
        store.mark_done(0)
        store2 = self.open_or_create()
        self.assertEqual(store2.next_pending(), 1)
        self.assertEqual(store2.records[0].status, STATUS_DONE)
        self.assertEqual(store2.records[0].attempts, 1)

    def test_corrupted_json(self):
        self.open_or_create()
        with open(self.path, "wb") as fh:
            fh.write(b'{"version": 1, "chunks": [')
        with self.assertRaises(CheckpointCorrupted):
            self.open_or_create()

    def test_garbage_file(self):
        with open(self.path, "wb") as fh:
            fh.write(os.urandom(64))
        with self.assertRaises(CheckpointCorrupted):
            self.open_or_create()

    def test_missing_keys(self):
        with open(self.path, "w") as fh:
            json.dump({"version": 1}, fh)
        with self.assertRaises(CheckpointCorrupted):
            self.open_or_create()

    def test_script_mismatch_refused(self):
        self.open_or_create()
        other = SQL + b" "
        chunks = split_and_chunk(other, 40)
        with self.assertRaises(CheckpointCorrupted):
            CheckpointStore.open_or_create(self.path, other, chunks, 40)

    def test_chunk_sha_mismatch_detected(self):
        store = self.open_or_create()
        data = json.load(open(self.path))
        data["chunks"][0]["sha256"] = "0" * 64
        with open(self.path, "w") as fh:
            json.dump(data, fh)
        with self.assertRaises(CheckpointCorrupted):
            self.open_or_create()

    def test_chunk_count_mismatch_detected(self):
        self.open_or_create()
        data = json.load(open(self.path))
        data["chunks"] = data["chunks"][:-1]
        with open(self.path, "w") as fh:
            json.dump(data, fh)
        with self.assertRaises(CheckpointCorrupted):
            self.open_or_create()

    def test_atomic_write_leaves_no_temp_files(self):
        store = self.open_or_create()
        store.mark_started(0)
        store.mark_done(0)
        leftovers = [f for f in os.listdir(self.dir) if f.startswith(".ckpt.")]
        self.assertEqual(leftovers, [])

    def test_progress_proof_fields(self):
        store = self.open_or_create()
        store.mark_started(0)
        store.mark_done(0)
        data = json.load(open(self.path))
        self.assertEqual(len(data["script_sha256"]), 64)
        rec = data["chunks"][0]
        for key in ("sha256", "status", "attempts", "retries", "byte_start",
                    "byte_end", "statements", "finished_at"):
            self.assertIn(key, rec)


if __name__ == "__main__":
    unittest.main()
