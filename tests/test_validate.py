import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

from icon_theme.validate import validate_icons, validate_theme  # noqa: E402
import gen_fixture  # noqa: E402


class ValidateThemeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.icons = gen_fixture.build_diff_fixture(cls.tmp.name)
        cls.theme = os.path.join(cls.icons, "maintheme")
        cls.report = validate_theme(cls.theme)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def messages(self, severity):
        return [p.message for p in self.report.problems
                if p.severity == severity]

    def test_size_dirs_detected(self):
        self.assertEqual(self.report.size_dirs, [16, 32, 48])

    def test_size_dir_case_error(self):
        errors = self.messages("error")
        self.assertTrue(any("48x48" in m and "lowercase" in m for m in errors))

    def test_context_dir_case_error(self):
        errors = self.messages("error")
        self.assertTrue(any("context directory case mismatch" in m
                            for m in errors))

    def test_missing_inherited_theme_warning(self):
        warnings = self.messages("warning")
        self.assertTrue(any("ghosttheme" in m for m in warnings))

    def test_corrupt_files_reported_separately(self):
        corrupt_paths = {os.path.basename(c.path) for c in self.report.corrupt}
        self.assertEqual(corrupt_paths,
                         {"broken.png", "allbroken.png", "allbroken.svg"})
        self.assertFalse(any("corrupt" in m for m in self.messages("error")))

    def test_uppercase_extension_info(self):
        infos = self.messages("info")
        self.assertTrue(any("case-insensitive" in m for m in infos))

    def test_duplicate_stem_winner_reported(self):
        infos = self.messages("info")
        dup = [m for m in infos if "resolution picks" in m]
        self.assertTrue(any("'both.png'" in m for m in dup))
        self.assertTrue(any("'prio.png'" in m for m in dup))

    def test_icon_file_count(self):
        self.assertEqual(self.report.icon_files, 17)


class ValidateIconsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.icons = gen_fixture.build_diff_fixture(cls.tmp.name)
        cls.theme = os.path.join(cls.icons, "maintheme")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_missing_and_corrupt_buckets_are_separate(self):
        queries = [
            ("exact16", 16, "apps"),     # hit
            ("ghost", 32, "apps"),       # clean missing
            ("allbroken", 32, "apps"),   # missing, corrupt only
            ("broken", 32, "apps"),      # hit after skipping corrupt
        ]
        results, summary = validate_icons(self.theme, queries)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["hits"], 2)
        self.assertEqual(summary["missing_clean"], 1)
        self.assertEqual(summary["missing_corrupt_only"], 1)
        self.assertEqual(summary["corrupt_files_seen"], 3)
        statuses = [r.status for r in results]
        self.assertEqual(statuses, ["hit", "missing", "missing", "hit"])


if __name__ == "__main__":
    unittest.main()
