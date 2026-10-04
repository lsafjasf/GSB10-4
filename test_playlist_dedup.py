"""playlist_dedup 自测:python3 -m unittest test_playlist_dedup -v"""

import os
import shutil
import stat
import tempfile
import unittest

from playlist_dedup import (
    DedupResult,
    Resolver,
    dedup_entries,
    naive_string_dedup,
)


class TempTreeMixin(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pdtest_")
        self.addCleanup(shutil.rmtree, self.root, True)
        # 基础结构
        #   music/a.mp3 music/b.mp3 sub/c.mp3 deep_link -> music(相对符号链接)
        #   loops/x -> y, loops/y -> x, loops/self -> self
        self.music = os.path.join(self.root, "music")
        self.sub = os.path.join(self.root, "sub")
        os.makedirs(self.music)
        os.makedirs(self.sub)
        os.makedirs(os.path.join(self.root, "loops"))
        self.a = os.path.join(self.music, "a.mp3")
        self.b = os.path.join(self.music, "b.mp3")
        self.c = os.path.join(self.sub, "c.mp3")
        for path in (self.a, self.b, self.c):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x")
        os.symlink("music", os.path.join(self.root, "deep_link"))  # 相对链接
        os.symlink("y", os.path.join(self.root, "loops", "x"))
        os.symlink("x", os.path.join(self.root, "loops", "y"))
        os.symlink("self", os.path.join(self.root, "loops", "self"))

    def rel(self, *parts):
        return os.path.join(*parts)


class TestAbsoluteOnly(TempTreeMixin):
    def test_all_absolute(self):
        entries = [(self.a, None), (self.b, None), (self.c, None)]
        result = dedup_entries(entries)
        self.assertEqual(len(result.unique), 3)
        self.assertEqual(len(result.duplicates), 0)
        self.assertEqual(len(result.errors), 0)
        self.assertEqual([u.index for u in result.unique], [0, 1, 2])
        for item in result.unique:
            self.assertTrue(os.path.isabs(item.real_path))
            self.assertNotIn("..", item.real_path.split(os.sep))

    def test_absolute_dotdot_converges(self):
        weird = os.path.join(self.music, "..", "music", ".", "a.mp3")
        result = dedup_entries([(self.a, None), (weird, None)])
        self.assertEqual(len(result.unique), 1)
        self.assertEqual(len(result.duplicates), 1)
        self.assertEqual(result.duplicates[0].merged_into, 0)


class TestAllDuplicates(TempTreeMixin):
    def test_all_duplicates_collapse_to_one(self):
        entries = [
            ("a.mp3", self.music),
            (self.a, None),
            (os.path.join(self.music, ".", "a.mp3"), None),
            (os.path.join(self.root, "deep_link", "a.mp3"), None),
            (os.path.join("..", "music", "a.mp3"), self.sub),
        ]
        result = dedup_entries(entries)
        self.assertEqual(len(result.unique), 1)
        self.assertEqual(len(result.duplicates), 4)
        self.assertEqual(len(result.errors), 0)
        for dup in result.duplicates:
            self.assertEqual(dup.merged_into, 0)
            self.assertEqual(dup.merged_into_path, "a.mp3")
            self.assertEqual(dup.real_path, result.unique[0].real_path)
        # 首现保留的是第一条的原始写法
        self.assertEqual(result.unique[0].path, "a.mp3")
        self.assertEqual(result.unique[0].base, self.music)


class TestMixedSpellings(TempTreeMixin):
    def test_mixed_spellings_and_order(self):
        entries = [
            (self.b, None),                                  # 绝对
            ("a.mp3", self.music),                           # 相对
            (os.path.join("..", "music", "b.mp3"), self.sub),  # .. 回退
            (os.path.join(self.root, "deep_link", "a.mp3"), None),  # 链接目录
            ("c.mp3", self.sub),
        ]
        result = dedup_entries(entries)
        self.assertEqual(len(result.errors), 0)
        self.assertEqual([u.path for u in result.unique],
                         [self.b, "a.mp3", "c.mp3"])  # 首次出现顺序
        self.assertEqual([d.merged_into for d in result.duplicates], [0, 1])
        # 链接目录写法与直接写法收敛到同一规范形式
        self.assertEqual(result.duplicates[1].real_path,
                         result.unique[1].real_path)

    def test_double_slash_spelling(self):
        doubled = self.music + os.sep + os.sep + "a.mp3"
        result = dedup_entries([(self.a, None), (doubled, None)])
        self.assertEqual(len(result.unique), 1)
        self.assertEqual(len(result.duplicates), 1)

    def test_hardlink_same_file(self):
        hard = os.path.join(self.sub, "a_hard.mp3")
        os.link(self.a, hard)
        result = dedup_entries([(self.a, None), (hard, None)])
        self.assertEqual(len(result.unique), 1)
        self.assertEqual(len(result.duplicates), 1)
        self.assertEqual(result.duplicates[0].merged_into, 0)

    def test_naive_baseline_differs(self):
        entries = [("a.mp3", self.music), (self.a, None)]
        self.assertEqual(len(naive_string_dedup(entries)), 2)   # 朴素:两条
        self.assertEqual(len(dedup_entries(entries).unique), 1)  # 规范:一条


class TestBaseRules(TempTreeMixin):
    def test_missing_base_rejected(self):
        result = dedup_entries([("a.mp3", None)])
        self.assertEqual(len(result.unique), 0)
        self.assertEqual(result.errors[0].code, "BASE_MISSING")

    def test_empty_base_rejected(self):
        result = dedup_entries([("a.mp3", "")])
        self.assertEqual(result.errors[0].code, "BASE_MISSING")

    def test_relative_base_rejected(self):
        result = dedup_entries([("a.mp3", "music")])
        self.assertEqual(result.errors[0].code, "BASE_INVALID")

    def test_base_not_found(self):
        result = dedup_entries([("a.mp3", os.path.join(self.root, "nope"))])
        self.assertEqual(result.errors[0].code, "BASE_NOT_FOUND")

    def test_base_not_a_directory(self):
        result = dedup_entries([("a.mp3", self.a)])
        self.assertEqual(result.errors[0].code, "BASE_NOT_A_DIRECTORY")

    def test_no_cwd_guessing(self):
        # 在 music 目录里 chdir 后,相对路径仍必须声明 base,不许按 cwd 猜
        old_cwd = os.getcwd()
        os.chdir(self.music)
        try:
            result = dedup_entries([("a.mp3", None)])
        finally:
            os.chdir(old_cwd)
        self.assertEqual(result.errors[0].code, "BASE_MISSING")
        # 声明 base 时与 cwd 无关
        ok = dedup_entries([("a.mp3", self.music)])
        self.assertEqual(len(ok.unique), 1)


class TestErrorClassification(TempTreeMixin):
    def test_not_found(self):
        result = dedup_entries([("ghost.mp3", self.music)])
        self.assertEqual(result.errors[0].code, "NOT_FOUND")

    def test_dangling_symlink_is_not_found(self):
        os.symlink("gone.mp3", os.path.join(self.music, "dangling.mp3"))
        result = dedup_entries([("dangling.mp3", self.music)])
        self.assertEqual(result.errors[0].code, "NOT_FOUND")

    def test_not_a_directory_component(self):
        result = dedup_entries([(os.path.join(self.a, "child.mp3"), None)])
        self.assertEqual(result.errors[0].code, "NOT_A_DIRECTORY")

    def test_permission_denied(self):
        if os.geteuid() == 0:
            self.skipTest("root 不受权限位限制")
        locked = os.path.join(self.root, "locked")
        os.makedirs(locked)
        with open(os.path.join(locked, "secret.mp3"), "w") as fh:
            fh.write("x")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, stat.S_IRWXU)
        result = dedup_entries([("secret.mp3", locked)])
        self.assertEqual(result.errors[0].code, "PERMISSION_DENIED")

    def test_invalid_path(self):
        result = dedup_entries([("", self.music)])
        self.assertEqual(result.errors[0].code, "INVALID_PATH")

    def test_errors_not_merged_together(self):
        entries = [("ghost1.mp3", self.music), ("ghost2.mp3", self.music)]
        result = dedup_entries(entries)
        self.assertEqual(len(result.errors), 2)
        self.assertEqual(len(result.unique), 0)
        self.assertEqual(len(result.duplicates), 0)


class TestSymlinkLoops(TempTreeMixin):
    def test_mutual_loop(self):
        result = dedup_entries([("x", os.path.join(self.root, "loops"))])
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].code, "SYMLINK_LOOP")

    def test_self_loop(self):
        result = dedup_entries([("self", os.path.join(self.root, "loops"))])
        self.assertEqual(result.errors[0].code, "SYMLINK_LOOP")

    def test_loop_via_absolute_target(self):
        os.symlink(os.path.join(self.root, "loops", "x"),
                   os.path.join(self.root, "loops", "abs"))
        result = dedup_entries([("abs", os.path.join(self.root, "loops"))])
        self.assertEqual(result.errors[0].code, "SYMLINK_LOOP")

    def test_loop_does_not_poison_other_entries(self):
        entries = [("x", os.path.join(self.root, "loops")),
                   ("a.mp3", self.music)]
        result = dedup_entries(entries)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(result.unique), 1)


class TestIntegrity(TempTreeMixin):
    def test_no_entry_lost_and_indexes_cover_all(self):
        entries = [
            ("a.mp3", self.music),
            (self.a, None),                      # dup of 0
            ("ghost.mp3", self.music),           # error
            ("b.mp3", self.music),
            (os.path.join(self.root, "deep_link", "b.mp3"), None),  # dup of 3
        ]
        result = dedup_entries(entries)
        self.assertEqual(result.total, len(entries))
        covered = sorted([u.index for u in result.unique]
                         + [d.index for d in result.duplicates]
                         + [e.index for e in result.errors])
        self.assertEqual(covered, list(range(len(entries))))
        # 重复项保留原始信息
        dup = result.duplicates[0]
        self.assertEqual((dup.path, dup.base), (self.a, None))
        self.assertEqual(dup.merged_into, 0)

    def test_case_sensitivity_matches_filesystem(self):
        upper = os.path.join(self.music, "A.MP3")
        result = dedup_entries([(self.a, None), (upper, None)])
        if os.path.exists(upper):  # 大小写不敏感文件系统
            self.assertEqual(len(result.unique), 1)
            self.assertEqual(len(result.duplicates), 1)
        else:                        # 大小写敏感:不同文件,不存在即报错
            self.assertEqual(len(result.unique), 1)
            self.assertEqual(result.errors[0].code, "NOT_FOUND")

    @unittest.skipIf(os.altsep is not None, "仅 POSIX:反斜杠属于文件名字符")
    def test_backslash_is_filename_char_on_posix(self):
        weird = os.path.join(self.music, r"a\b.mp3")
        with open(weird, "w") as fh:
            fh.write("x")
        result = dedup_entries([(r"a\b.mp3", self.music)])
        self.assertEqual(len(result.unique), 1)
        self.assertEqual(result.unique[0].real_path, weird)
        # 不会被切成 a 目录下的 b.mp3
        self.assertEqual(dedup_entries([(r"a\b.mp3", self.music),
                                        (weird, None)]).duplicates.__len__(), 1)


if __name__ == "__main__":
    unittest.main()
