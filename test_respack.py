"""respack 自测: python3 test_respack.py -v"""
import os
import shutil
import struct
import tempfile
import unittest

import respack
from respack import PackError


def make_entries():
    return [
        ("first.bin", b"A" * 100),
        ("mid.bin", bytes(range(256))),
        ("last.bin", b"Z" * 64),
    ]


class RespackTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="respack_test_")
        self.pack = os.path.join(self.dir, "test.rpk")
        respack.build_pack(self.pack, make_entries())

    def tearDown(self):
        shutil.rmtree(self.dir)

    def snapshot(self):
        dst = os.path.join(self.dir, "snapshot.rpk")
        shutil.copyfile(self.pack, dst)
        return dst

    # ---------- 基本解析 ----------
    def test_parse_roundtrip(self):
        idx = respack.read_index(self.pack, verify_crc=True)
        self.assertEqual([e.name for e in idx.entries],
                         ["first.bin", "mid.bin", "last.bin"])
        self.assertEqual([e.length for e in idx.entries], [100, 256, 64])

    # ---------- 首条目替换(追加型, 变大) ----------
    def test_replace_first_append_grow(self):
        snap = self.snapshot()
        rep = respack.replace(self.pack, "first.bin", b"N" * 300, "append")
        self.assertEqual(rep.new_length, 300)
        self.assertEqual(rep.bytes_written, 300 + rep.index_bytes_written)
        info = respack.verify_others_unchanged(snap, self.pack, "first.bin")
        self.assertEqual(info["entries_checked"], 2)
        idx = respack.read_index(self.pack, verify_crc=True)
        self.assertEqual(idx.by_name("first.bin").length, 300)

    # ---------- 末条目替换(追加型, 变小) ----------
    def test_replace_last_append_shrink(self):
        snap = self.snapshot()
        rep = respack.replace(self.pack, "last.bin", b"s" * 10, "append")
        self.assertEqual(rep.new_length, 10)
        respack.verify_others_unchanged(snap, self.pack, "last.bin")
        respack.read_index(self.pack, verify_crc=True)

    # ---------- 中间条目原地替换(等长与变小) ----------
    def test_replace_mid_inplace_same_and_shrink(self):
        snap = self.snapshot()
        rep = respack.replace(self.pack, "mid.bin", b"M" * 256, "inplace")
        self.assertEqual(rep.new_offset, rep.old_offset)
        respack.verify_others_unchanged(snap, self.pack, "mid.bin")
        snap2 = self.snapshot()
        rep2 = respack.replace(self.pack, "mid.bin", b"m" * 10, "inplace")
        self.assertEqual(rep2.new_length, 10)
        respack.verify_others_unchanged(snap2, self.pack, "mid.bin")
        respack.read_index(self.pack, verify_crc=True)

    # ---------- 原地型拒绝变大 ----------
    def test_inplace_grow_rejected(self):
        with self.assertRaises(PackError) as cm:
            respack.replace(self.pack, "last.bin", b"G" * 1000, "inplace")
        self.assertEqual(cm.exception.entry, "last.bin")

    # ---------- 差异区间只落在允许区域 ----------
    def test_diff_regions_classified(self):
        snap = self.snapshot()
        respack.replace(self.pack, "mid.bin", b"X" * 500, "append")
        info = respack.verify_others_unchanged(snap, self.pack, "mid.bin")
        labels = {r["region"] for r in info["regions"]}
        self.assertTrue(labels <= {"index-record", "target-old-data", "target-new-data"})
        self.assertIn("index-record", labels)
        self.assertIn("target-new-data", labels)

    # ---------- 区间重叠 ----------
    def test_overlap_rejected(self):
        idx = respack.read_index(self.pack)
        victim = idx.entries[1]  # mid.bin: 把它的 offset 改到 first.bin 区间内
        with open(self.pack, "r+b") as f:
            f.seek(victim.rec_pos + victim.rec_len - 4 - 8 - 8)  # offset 字段
            f.write(struct.pack("<Q", idx.entries[0].offset + 10))
        with self.assertRaises(PackError) as cm:
            respack.read_index(self.pack)
        self.assertIn("重叠", str(cm.exception))
        self.assertEqual(cm.exception.entry, "mid.bin")

    # ---------- 长度越界 ----------
    def test_length_out_of_bounds_rejected(self):
        idx = respack.read_index(self.pack)
        victim = idx.entries[2]  # last.bin
        with open(self.pack, "r+b") as f:
            f.seek(victim.rec_pos + victim.rec_len - 4 - 8)  # length 字段
            f.write(struct.pack("<Q", 10 ** 9))
        with self.assertRaises(PackError) as cm:
            respack.read_index(self.pack)
        self.assertIn("越界", str(cm.exception))
        self.assertEqual(cm.exception.entry, "last.bin")

    # ---------- 索引损坏 ----------
    def test_corrupt_magic_rejected(self):
        with open(self.pack, "r+b") as f:
            f.write(b"XXXX")
        with self.assertRaises(PackError) as cm:
            respack.read_index(self.pack)
        self.assertIn("magic", str(cm.exception))

    def test_truncated_index_rejected(self):
        size = os.path.getsize(self.pack)
        with open(self.pack, "r+b") as f:
            f.truncate(respack.HEADER.size + 5)  # 索引记录被截断
        with self.assertRaises(PackError) as cm:
            respack.read_index(self.pack)
        self.assertIn("截断", str(cm.exception))

    def test_bad_data_offset_rejected(self):
        with open(self.pack, "r+b") as f:
            f.seek(12)
            f.write(struct.pack("<Q", 4))  # data_offset 小于索引区末尾
        with self.assertRaises(PackError) as cm:
            respack.read_index(self.pack)
        self.assertIn("data_offset", str(cm.exception))

    # ---------- 条目不存在 ----------
    def test_missing_entry(self):
        with self.assertRaises(PackError) as cm:
            respack.replace(self.pack, "nope.bin", b"x", "append")
        self.assertEqual(cm.exception.entry, "nope.bin")


if __name__ == "__main__":
    unittest.main()
