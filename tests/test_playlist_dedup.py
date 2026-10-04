"""playlist_dedup 自测：覆盖全绝对、全重复、混合写法、链接成环及错误分类。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playlist_dedup import (  # noqa: E402
    ErrorKind,
    dedup_playlist,
    naive_string_dedup,
    validate_base_dir,
)


class PlaylistCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pldedup_")
        self.root = Path(self._tmp.name)
        (self.root / "music" / "rock").mkdir(parents=True)
        (self.root / "music" / "pop").mkdir(parents=True)
        self.song_a = self.root / "music" / "rock" / "a.mp3"
        self.song_b = self.root / "music" / "rock" / "b.mp3"
        self.song_c = self.root / "music" / "pop" / "c.mp3"
        for song in (self.song_a, self.song_b, self.song_c):
            song.write_bytes(b"audio")
        # 符号链接：目录别名 + 文件别名
        os.symlink(self.root / "music", self.root / "music_link")
        os.symlink(self.song_a, self.root / "a_alias.mp3")
        # 硬链接（同 inode 不同路径）
        os.link(self.song_b, self.root / "music" / "rock" / "b_hard.mp3")
        # 符号链接成环
        os.symlink(self.root / "loop2", self.root / "loop1")
        os.symlink(self.root / "loop1", self.root / "loop2")
        # 权限不足目录
        self.locked = self.root / "locked"
        self.locked.mkdir()
        (self.locked / "secret.mp3").write_bytes(b"x")
        self.locked.chmod(0o000)

    def tearDown(self):
        self.locked.chmod(0o700)
        self._tmp.cleanup()

    def rel(self, *parts):
        return str(Path(*parts))

    # ---------- 情形 1：全部绝对路径 ----------
    def test_all_absolute_distinct(self):
        items = [str(self.song_a), str(self.song_b), str(self.song_c)]
        result = dedup_playlist(items)  # 绝对路径不需要基准目录
        self.assertEqual(len(result.unique), 3)
        self.assertEqual(result.duplicates, [])
        self.assertEqual(result.errors, [])
        self.assertEqual([e.index for e in result.unique], [0, 1, 2])

    # ---------- 情形 2：全部重复（同一文件多种写法） ----------
    def test_all_duplicates_of_one_file(self):
        base = str(self.root)
        items = [
            str(self.song_a),                                        # 绝对
            self.rel("music", "rock", "a.mp3"),                      # 相对
            self.rel("music", ".", "rock", "a.mp3"),                 # 含 .
            self.rel("music", "pop", "..", "rock", "a.mp3"),         # 含 ..
            str(self.root / "music" / "rock" / "." / "a.mp3"),       # 绝对含 .
            str(self.root / "music" / "pop" / ".." / "rock" / "a.mp3"),
            str(self.root / "music_link" / "rock" / "a.mp3"),        # 目录符号链接
            str(self.root / "a_alias.mp3"),                          # 文件符号链接
            str(self.root / "music" / "rock" / "a.mp3"),             # 字符串完全重复
        ]
        result = dedup_playlist(items, base_dir=base)
        self.assertEqual(len(result.unique), 1)
        self.assertEqual(len(result.duplicates), 8)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.unique[0].index, 0)
        for dup in result.duplicates:
            self.assertEqual(dup.merged_into, 0)
            self.assertEqual(dup.canonical, result.unique[0].canonical)
        self.assertEqual([e.index for e in result.duplicates], list(range(1, 9)))

    # ---------- 情形 3：混合写法，多文件 ----------
    def test_mixed_forms_multiple_files(self):
        base = str(self.root)
        items = [
            self.rel("music", "rock", "a.mp3"),       # 0 -> a
            str(self.song_b),                          # 1 -> b
            str(self.root / "music_link" / "rock" / "a.mp3"),  # 2 -> a dup of 0
            self.rel("music", "rock", "b_hard.mp3"),  # 3 -> b 硬链接 dup of 1
            str(self.song_c),                          # 4 -> c
            str(self.root / "a_alias.mp3"),            # 5 -> a dup of 0
        ]
        result = dedup_playlist(items, base_dir=base)
        self.assertEqual([e.index for e in result.unique], [0, 1, 4])
        merges = {e.index: e.merged_into for e in result.duplicates}
        self.assertEqual(merges, {2: 0, 3: 1, 5: 0})
        self.assertEqual(
            [Path(e.canonical).name for e in result.unique],
            ["a.mp3", "b.mp3", "c.mp3"],
        )

    # ---------- 情形 4：符号链接成环 ----------
    def test_symlink_loop_classified(self):
        items = [str(self.root / "loop1"), str(self.root / "loop2" / "x.mp3")]
        result = dedup_playlist(items)
        self.assertEqual(len(result.errors), 2)
        for entry in result.errors:
            self.assertEqual(entry.error.kind, ErrorKind.SYMLINK_LOOP)

    # ---------- 基准目录规则 ----------
    def test_relative_without_base_rejected(self):
        result = dedup_playlist([self.rel("music", "rock", "a.mp3")], base_dir=None)
        self.assertEqual(result.errors[0].error.kind, ErrorKind.MISSING_BASE)
        self.assertIn("拒绝按当前工作目录猜测", result.errors[0].error.message)

    def test_base_dir_missing(self):
        result = dedup_playlist(
            [self.rel("music", "rock", "a.mp3")],
            base_dir=str(self.root / "no_such_dir"),
        )
        self.assertEqual(result.errors[0].error.kind, ErrorKind.BASE_NOT_FOUND)

    def test_base_not_directory(self):
        result = dedup_playlist([self.rel("a.mp3")], base_dir=str(self.song_a))
        self.assertEqual(result.errors[0].error.kind, ErrorKind.BASE_NOT_DIRECTORY)

    def test_absolute_entries_unaffected_by_bad_base(self):
        result = dedup_playlist(
            [str(self.song_a), self.rel("music", "rock", "a.mp3")],
            base_dir=str(self.root / "no_such_dir"),
        )
        self.assertEqual(len(result.unique), 1)   # 绝对路径照常解析
        self.assertEqual(len(result.errors), 1)   # 相对路径因基准失效被拒

    # ---------- 错误分类：不存在 / 非目录 / 权限 ----------
    def test_not_found(self):
        result = dedup_playlist([str(self.root / "music" / "rock" / "ghost.mp3")])
        self.assertEqual(result.errors[0].error.kind, ErrorKind.NOT_FOUND)

    def test_not_a_directory_component(self):
        result = dedup_playlist([str(self.song_a / "inner.mp3")])
        self.assertEqual(result.errors[0].error.kind, ErrorKind.NOT_A_DIRECTORY)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root 绕过文件权限，无法触发 EACCES")
    def test_permission_denied(self):
        result = dedup_playlist([str(self.locked / "secret.mp3")])
        self.assertEqual(result.errors[0].error.kind, ErrorKind.PERMISSION_DENIED)

    def test_empty_path(self):
        result = dedup_playlist([""])
        self.assertEqual(result.errors[0].error.kind, ErrorKind.EMPTY_PATH)

    def test_error_kinds_are_distinct(self):
        items = [
            str(self.root / "ghost.mp3"),       # NOT_FOUND
            str(self.song_a / "inner.mp3"),     # NOT_A_DIRECTORY
            str(self.root / "loop1"),           # SYMLINK_LOOP
            self.rel("x.mp3"),                  # MISSING_BASE
        ]
        result = dedup_playlist(items)  # 无基准
        kinds = [e.error.kind for e in result.errors]
        self.assertEqual(
            kinds,
            [ErrorKind.NOT_FOUND, ErrorKind.NOT_A_DIRECTORY,
             ErrorKind.SYMLINK_LOOP, ErrorKind.MISSING_BASE],
        )
        self.assertEqual(len(set(kinds)), 4)  # 分类不混淆

    # ---------- 去重语义 ----------
    def test_first_occurrence_order_preserved(self):
        base = str(self.root)
        items = [
            self.rel("music", "pop", "c.mp3"),
            str(self.song_a),
            self.rel("music", "rock", "b.mp3"),
            str(self.song_c),  # dup of 0
        ]
        result = dedup_playlist(items, base_dir=base)
        self.assertEqual([e.index for e in result.unique], [0, 1, 2])
        self.assertEqual(result.duplicates[0].merged_into, 0)

    def test_raw_preserved(self):
        raw = self.rel("music", ".", "rock", "a.mp3")
        result = dedup_playlist([raw], base_dir=str(self.root))
        self.assertEqual(result.unique[0].raw, raw)
        self.assertTrue(os.path.isabs(result.unique[0].canonical))

    def test_differs_from_naive_string_dedup(self):
        base = str(self.root)
        items = [
            str(self.song_a),
            self.rel("music", "rock", "a.mp3"),
            str(self.root / "music_link" / "rock" / "a.mp3"),
            str(self.song_b),
        ]
        result = dedup_playlist(items, base_dir=base)
        naive_unique, _ = naive_string_dedup(items)
        self.assertEqual(len(result.unique), 2)
        self.assertEqual(len(naive_unique), 4)
        self.assertEqual(len(naive_unique) - len(result.unique), 2)

    def test_validate_base_dir_ok(self):
        self.assertIsNone(validate_base_dir(str(self.root)))
        self.assertIsNone(validate_base_dir(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
