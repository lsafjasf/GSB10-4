import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

from icon_theme.theme import DirCache, Resolver  # noqa: E402
import gen_fixture  # noqa: E402


class ResolveChainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.icons = gen_fixture.build_diff_fixture(cls.tmp.name)
        cls.theme = os.path.join(cls.icons, "maintheme")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def resolve(self, name, size, context="apps"):
        return Resolver(self.theme).resolve(name, size, context)

    def test_exact_size_hit(self):
        res = self.resolve("exact16", 16)
        self.assertEqual(res.status, "hit")
        self.assertEqual((res.hit.stage, res.hit.level), ("main", 1))
        self.assertTrue(res.hit.path.endswith("16x16/apps/exact16.png"))

    def test_larger_size_fallback(self):
        res = self.resolve("multi", 32)
        self.assertEqual((res.hit.stage, res.hit.level), ("main", 2))
        self.assertTrue(res.hit.path.endswith("48x48/apps/multi.png"))

    def test_smaller_size_fallback(self):
        res = self.resolve("multi", 64)
        self.assertEqual((res.hit.stage, res.hit.level), ("main", 3))
        self.assertTrue(res.hit.path.endswith("48x48/apps/multi.png"))

    def test_only_low_resolution_still_hits(self):
        res = self.resolve("onlylow", 48)
        self.assertEqual(res.status, "hit")
        self.assertEqual(res.hit.level, 3)
        self.assertTrue(res.hit.path.endswith("16x16/apps/onlylow.png"))

    def test_scalable_fallback(self):
        res = self.resolve("vec", 32)
        self.assertEqual(res.hit.level, 4)
        self.assertTrue(res.hit.path.endswith("scalable/apps/vec.svg"))

    def test_unthemed_root_fallback(self):
        res = self.resolve("rooticon", 32)
        self.assertEqual(res.hit.level, 5)
        self.assertTrue(res.hit.path.endswith("maintheme/rooticon.png"))

    def test_inherited_theme_fallback(self):
        res = self.resolve("fromparent", 32)
        self.assertEqual(res.hit.stage, "inherited")
        self.assertTrue(res.hit.path.endswith("parenttheme/32x32/apps/fromparent.png"))

    def test_hicolor_is_last_resort(self):
        res = self.resolve("fallback", 32)
        self.assertEqual(res.hit.stage, "hicolor")
        stages = [e.stage for e in res.chain]
        self.assertEqual(stages.index("hicolor"), len(stages) - 1)

    def test_all_missing(self):
        res = self.resolve("ghost", 32)
        self.assertEqual(res.status, "missing")
        self.assertIsNone(res.hit)
        self.assertEqual(res.corrupt, [])

    def test_corrupt_not_counted_as_hit_and_reported(self):
        res = self.resolve("broken", 32)
        self.assertEqual(res.status, "hit")
        self.assertEqual(res.hit.level, 2)
        self.assertEqual(len(res.corrupt), 1)
        self.assertTrue(res.corrupt[0].path.endswith("32x32/apps/broken.png"))
        self.assertIn("PNG", res.corrupt[0].reason)

    def test_all_corrupt_reports_missing_with_corrupt_list(self):
        res = self.resolve("allbroken", 32)
        self.assertEqual(res.status, "missing")
        self.assertEqual(len(res.corrupt), 2)

    def test_uppercase_extension_matches(self):
        res = self.resolve("caseext", 32)
        self.assertEqual(res.status, "hit")
        self.assertTrue(res.hit.path.endswith("caseext.PNG"))

    def test_lowercase_extension_wins_over_uppercase(self):
        res = self.resolve("both", 32)
        self.assertTrue(res.hit.path.endswith(os.path.join("apps", "both.png")))

    def test_extension_priority_png_before_svg(self):
        res = self.resolve("prio", 32)
        self.assertTrue(res.hit.path.endswith("prio.png"))

    def test_context_dir_is_case_sensitive(self):
        res = self.resolve("wrongcase", 32)
        self.assertEqual(res.status, "missing")

    def test_size_dir_is_case_sensitive(self):
        res = self.resolve("casedir", 48)
        self.assertEqual(res.status, "missing")

    def test_chain_records_every_level_tried(self):
        res = self.resolve("fallback", 32)
        levels = [(e.stage, e.level) for e in res.chain]
        self.assertIn(("main", 1), levels)
        self.assertIn(("main", 5), levels)
        self.assertIn(("inherited", 1), levels)
        self.assertIn(("hicolor", 1), levels)

    def test_shared_cache_avoids_rescanning(self):
        cache = DirCache()
        resolver = Resolver(self.theme, cache=cache)
        resolver.resolve("ghost", 32, "apps")
        first = cache.scandir_count
        resolver.resolve("ghost", 32, "apps")
        self.assertEqual(cache.scandir_count, first)


class EdgeCaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, data):
        path = os.path.join(self.base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_inherits_cycle_terminates(self):
        self.write("icons/ta/index.theme",
                   b"[Icon Theme]\nInherits=tb\n")
        self.write("icons/tb/index.theme",
                   b"[Icon Theme]\nInherits=ta\n")
        res = Resolver(os.path.join(self.base, "icons", "ta")).resolve("x", 32, "apps")
        self.assertEqual(res.status, "missing")

    def test_self_inherit_terminates(self):
        self.write("icons/tc/index.theme", b"[Icon Theme]\nInherits=tc\n")
        res = Resolver(os.path.join(self.base, "icons", "tc")).resolve("x", 32, "apps")
        self.assertEqual(res.status, "missing")

    def test_scalable_level_accepts_svg_only(self):
        self.write("icons/td/index.theme", b"[Icon Theme]\n")
        self.write("icons/td/scalable/apps/vec.png", gen_fixture.VALID_PNG)
        res = Resolver(os.path.join(self.base, "icons", "td")).resolve("vec", 32, "apps")
        self.assertEqual(res.status, "missing")

    def test_empty_file_is_corrupt(self):
        self.write("icons/te/index.theme", b"[Icon Theme]\n")
        self.write("icons/te/32x32/apps/e.png", b"")
        res = Resolver(os.path.join(self.base, "icons", "te")).resolve("e", 32, "apps")
        self.assertEqual(res.status, "missing")
        self.assertEqual(res.corrupt[0].reason, "empty file")

    def test_missing_theme_dir_is_skip_not_crash(self):
        res = Resolver(os.path.join(self.base, "icons", "nope")).resolve("x", 32, "apps")
        self.assertEqual(res.status, "missing")
        self.assertEqual(res.chain[0].outcome, "skip: theme directory unreadable")

    def test_missing_inherited_theme_is_skipped(self):
        self.write("icons/tf/index.theme", b"[Icon Theme]\nInherits=ghosttheme\n")
        self.write("icons/tf/32x32/apps/a.png", gen_fixture.VALID_PNG)
        res = Resolver(os.path.join(self.base, "icons", "tf")).resolve("a", 32, "apps")
        self.assertEqual(res.status, "hit")
        res2 = Resolver(os.path.join(self.base, "icons", "tf")).resolve("b", 32, "apps")
        self.assertEqual(res2.status, "missing")
        skips = [e for e in res2.chain if e.outcome.startswith("skip")]
        self.assertTrue(any("ghosttheme" in e.detail for e in skips))


class DiffDataTest(unittest.TestCase):
    """对拍：库输出 vs data/diff_cases.json 中手工核对的预期。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.icons = gen_fixture.build_diff_fixture(cls.tmp.name)
        with open(os.path.join(REPO_ROOT, "data", "diff_cases.json"),
                  encoding="utf-8") as fh:
            cls.data = json.load(fh)
        cls.resolver = Resolver(os.path.join(cls.icons, cls.data["theme"]))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_cases_match_hand_checked_expectations(self):
        failures = []
        for case in self.data["cases"]:
            q, exp = case["query"], case["expect"]
            res = self.resolver.resolve(q["name"], q["size"], q["context"])
            problems = []
            if res.status != exp["status"]:
                problems.append(f"status={res.status}")
            if len(res.corrupt) != exp["corrupt"]:
                problems.append(f"corrupt={len(res.corrupt)}")
            if exp["status"] == "hit" and res.hit:
                hit = res.hit
                if (hit.stage, hit.level, hit.level_name) != (
                        exp["stage"], exp["level"], exp["level_name"]):
                    problems.append(
                        f"level=({hit.stage},{hit.level},{hit.level_name})")
                if not hit.path.replace(os.sep, "/").endswith(exp["path_suffix"]):
                    problems.append(f"path={hit.path}")
            if problems:
                failures.append(f"{case['id']}: {', '.join(problems)}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
