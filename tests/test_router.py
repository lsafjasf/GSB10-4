"""test_router.py -- 路由库自测（unittest，仅标准库）。

覆盖：
* 空规则集 / 单条规则 / 多条同优先级冲突（含三路冲突，全部列出）
* 显式优先级压过最长前缀
* 通配（段内 glob、整段 '*'）与转义（\\*、\\\\、非法转义）混用
* 规则去重、路径规范化
* 对拍：7 条规则 5040 种加载排列 x 13 查询，与独立朴素实现及夹具逐字节比对
* 随机 fuzz：随机规则/查询、多次乱序加载，对拍朴素实现
"""

from __future__ import annotations

import itertools
import json
import os
import random
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

from router import (  # noqa: E402
    DuplicateRuleError,
    PatternError,
    RouteConflict,
    Rule,
    Router,
    parse_pattern,
)
from naive_matcher import NaiveRouter, NaiveRule  # noqa: E402


def ordered_hits(result):
    return [h.rule_id for h in result.matches]


def naive_ordered(rules_by_id, hit_ids, top):
    if not hit_ids:
        return [], []
    from make_fixture import specificity
    ordered = sorted(
        hit_ids,
        key=lambda i: (
            -rules_by_id[i]["priority"],
            tuple(-x for x in specificity(rules_by_id[i]["pattern"])),
            i,
        ),
    )
    conflicting = sorted(i for i in hit_ids if rules_by_id[i]["priority"] == top)
    return ordered, conflicting


class BasicTests(unittest.TestCase):
    def test_empty_ruleset(self):
        router = Router([])
        for path in ("/", "/anything", "/a/b/c", ""):
            result = router.evaluate(path)
            self.assertEqual(result.status, "no_match", path)
            self.assertIsNone(router.match(path))
        self.assertEqual(router.rule_count, 0)
        self.assertGreaterEqual(router.index_memory_bytes(), 0)

    def test_single_rule(self):
        router = Router([Rule("/users", priority=1, id="users")])
        result = router.evaluate("/users")
        self.assertEqual(result.status, "match")
        self.assertEqual(result.winner.rule_id, "users")
        self.assertEqual(ordered_hits(result), ["users"])
        # 前缀语义：更长的请求路径也命中
        self.assertEqual(router.match("/users/42").rule_id, "users")
        # 根 '/' 命中一切（唯一规则无冲突）
        root = Router([Rule("/", id="root")])
        self.assertEqual(root.match("/x/y/z").rule_id, "root")

    def test_same_priority_pair_conflict_lists_all(self):
        router = Router([
            Rule("/a", priority=1, id="rA"),
            Rule("/a/b", priority=1, id="rAB"),
        ])
        result = router.evaluate("/a/b")
        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.top_priority, 1)
        # 必须列出全部命中项，而不是只报一条
        self.assertEqual(ordered_hits(result), ["rAB", "rA"])
        with self.assertRaises(RouteConflict) as ctx:
            router.match("/a/b")
        self.assertEqual([h.rule_id for h in ctx.exception.conflicting], ["rAB", "rA"])
        self.assertIn("rAB", result.report())
        self.assertIn("rA", result.report())

    def test_three_way_conflict(self):
        router = Router([
            Rule("/a", priority=2, id="a"),
            Rule("/a/b", priority=2, id="ab"),
            Rule("/a/b/c", priority=2, id="abc"),
        ])
        result = router.evaluate("/a/b/c")
        self.assertEqual(result.status, "conflict")
        self.assertEqual(ordered_hits(result), ["abc", "ab", "a"])  # specificity 降序
        report = result.report()
        self.assertIn("conflict_count: 3", report)

    def test_explicit_priority_beats_longest_prefix(self):
        # 低优先级但最长前缀 vs 高优先级短前缀：高优先级必须胜（依据见 PRIORITY.md）
        router = Router([
            Rule("/a/b/c", priority=0, id="long-but-low"),
            Rule("/a", priority=10, id="short-but-high"),
        ])
        result = router.evaluate("/a/b/c")
        self.assertEqual(result.status, "match")
        self.assertEqual(result.winner.rule_id, "short-but-high")
        self.assertEqual(ordered_hits(result), ["short-but-high", "long-but-low"])

    def test_lower_priority_rules_still_listed(self):
        router = Router([
            Rule("/a", priority=1, id="low"),
            Rule("/a", priority=9, id="high"),
        ])
        result = router.evaluate("/a")
        self.assertEqual(result.status, "match")
        self.assertEqual(ordered_hits(result), ["high", "low"])

    def test_wildcard_segment_and_in_glob(self):
        router = Router([
            Rule("/files/*", priority=1, id="seg"),   # 整段 *：匹配任意一个非空段
            Rule("/files/pre*", priority=2, id="pre"),
            Rule("/files/*suf", priority=3, id="suf"),
            Rule("/files/m*d", priority=4, id="mid"),
        ])
        self.assertEqual(router.evaluate("/files/x").winner.rule_id, "seg")
        self.assertEqual(router.evaluate("/files/preXYZ").winner.rule_id, "pre")
        self.assertEqual(router.evaluate("/files/XYZsuf").winner.rule_id, "suf")
        self.assertEqual(router.evaluate("/files/mXYZd").winner.rule_id, "mid")
        # '*' 匹配空串：pre 也命中 "pre"；与 seg 同时命中，高优先级 pre 胜
        result = router.evaluate("/files/pre")
        self.assertIn("pre", ordered_hits(result))
        self.assertIn("seg", ordered_hits(result))
        self.assertEqual(result.winner.rule_id, "pre")
        # 不跨段：整段 * 只占一个段；少一段则不命中
        self.assertEqual(router.evaluate("/files").status, "no_match")

    def test_escaped_star_is_literal(self):
        # pattern: /files/a\*b 表示字面 "a*b"，绝不是通配
        router = Router([
            Rule("/files/a\\*b", priority=1, id="litstar"),
            Rule("/files/*", priority=1, id="glob"),
        ])
        result = router.evaluate("/files/a*b")
        self.assertEqual([h.rule_id for h in result.matches
                          if h.rule_id == "litstar"], ["litstar"])
        self.assertIn("glob", ordered_hits(result))  # glob 也命中 -> 同优先级冲突
        # 字面规则不得把 '*' 当通配：这些请求只命中 glob
        self.assertEqual(
            [h.rule_id for h in router.evaluate("/files/aXb").matches
             if h.rule_id == "litstar"], [],
        )

    def test_escaped_backslash_is_literal(self):
        # pattern /r/\\x（第二段源码 r/\\\\x：两个反斜杠转义为一个字面反斜杠）
        router = Router([Rule("/r/\\\\x", id="rb")])
        self.assertEqual(router.match("/r/\\x").rule_id, "rb")
        self.assertIsNone(router.match("/r/x"))
        self.assertIsNone(router.match("/r/xx"))

    def test_invalid_escapes_rejected_at_load(self):
        for bad in ("/a\\q", "/a\\", "/a\\q/b", "/\\/"):
            with self.assertRaises(PatternError, msg=bad):
                Router([Rule(bad)])

    def test_duplicate_rule_rejected(self):
        with self.assertRaises(DuplicateRuleError):
            Router([Rule("/a", priority=1), Rule("/a", priority=1)])
        # 同 pattern 不同 priority 合法
        Router([Rule("/a", priority=1), Rule("/a", priority=2)])

    def test_slash_normalization_equivalent(self):
        r1 = Router([Rule("/a/b", id="x")])
        r2 = Router([Rule("/a//b/", id="x")])
        r3 = Router([Rule("a/b/", id="x")])
        for path in ("/a/b", "/a//b//", "a/b/", "//a//b//"):
            for r in (r1, r2, r3):
                self.assertEqual(r.match(path).rule_id, "x", (path,))

    def test_auto_ids_stable_across_orders(self):
        rules = [Rule("/a", 0), Rule("/b", 0), Rule("/a/b", 0)]
        hit_orders = []
        for perm in itertools.permutations(rules):
            res = Router(list(perm)).evaluate("/a/b")
            hit_orders.append(ordered_hits(res))
        self.assertEqual(len({tuple(h) for h in hit_orders}), 1)


class DifferentialTests(unittest.TestCase):
    """对拍：全部 N! 加载排列 x 全部查询，结果与夹具/朴素实现一致。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "data", "differential_fixture.json"),
                  encoding="utf-8") as fh:
            cls.fixture = json.load(fh)
        cls.specs = cls.fixture["rules"]
        cls.rules_by_id = {r["id"]: r for r in cls.specs}

    def _check_result(self, result, expected):
        self.assertEqual(result.status, expected["status"], expected["path"])
        self.assertEqual(result.top_priority, expected["top_priority"], expected["path"])
        winner = result.winner.rule_id if result.winner else None
        self.assertEqual(winner, expected["winner"], expected["path"])
        self.assertEqual(ordered_hits(result), expected["hits_ordered"], expected["path"])
        conflicting = [h.rule_id for h in result.matches
                       if h.priority == result.top_priority] if result.status == "conflict" else []
        self.assertEqual(sorted(conflicting), expected["conflicting"], expected["path"])

    def test_all_permutations_against_fixture_and_naive(self):
        base = [Rule(r["pattern"], r["priority"], r["id"]) for r in self.specs]
        naive = NaiveRouter(
            [NaiveRule(r["pattern"], r["priority"], r["id"]) for r in self.specs]
        )
        count = 0
        for perm in itertools.permutations(base):
            router = Router(list(perm))
            for expected in self.fixture["queries"]:
                path = expected["path"]
                self._check_result(router.evaluate(path), expected)
                # 朴素实现交叉验证命中集合与最高优先级
                nev = naive.evaluate(path)
                trie_hits = set(ordered_hits(router.evaluate(path)))
                self.assertEqual(trie_hits, set(nev["hits"]), (perm, path))
                self.assertEqual(router.evaluate(path).top_priority,
                                 nev["top_priority"], path)
            count += 1
        self.assertEqual(count, self.fixture["permutations_tested"])

    def test_fuzz_against_naive(self):
        rng = random.Random(20261005)
        lit_segments = ["a", "b", "ab", "x", ""]  # "" 经 split 后被丢弃
        glob_segments = ["*", "a*", "*b", "a*b", "*x*", "ab*"]
        escaped_segments = ["a\\*b", "c\\\\d", "\\*", "\\\\"]
        seg_pool = lit_segments + glob_segments + escaped_segments

        for trial in range(60):
            n_rules = rng.randint(1, 14)
            specs = {}
            patterns = set()
            while len(specs) < n_rules:
                depth = rng.randint(1, 3)
                pat = "/" + "/".join(rng.choice(seg_pool) for _ in range(depth))
                pri = rng.choice([0, 0, 1, 2, 5])
                # 按编译后形态去重：不同原始串（如 a//b 与 a/b）可能规范化为同一条规则
                compiled_key = (parse_pattern(pat), pri)
                if compiled_key in patterns:
                    continue
                patterns.add(compiled_key)
                rid = "id%d" % len(specs)
                specs[rid] = {"pattern": pat, "priority": pri}
            ids = list(specs)
            rules = [Rule(specs[i]["pattern"], specs[i]["priority"], i) for i in ids]
            naive = NaiveRouter(
                [NaiveRule(specs[i]["pattern"], specs[i]["priority"], i) for i in ids]
            )
            query_segs = ["a", "b", "ab", "*", "\\", "a*b", "axb", "xyz", ""]
            queries = [
                "/" + "/".join(rng.choice(query_segs) for _ in range(rng.randint(0, 4)))
                for _ in range(40)
            ] + ["/", "", "/a/b/c/d/e"]

            seen_signatures = set()
            for shuffle in range(6):
                shuffled = rules[:]
                rng.shuffle(shuffled)
                router = Router(shuffled)
                sig_parts = []
                for path in queries:
                    result = router.evaluate(path)
                    nev = naive.evaluate(path)
                    self.assertEqual(
                        set(ordered_hits(result)), set(nev["hits"]),
                        (trial, shuffle, path, specs),
                    )
                    self.assertEqual(result.top_priority, nev["top_priority"],
                                     (trial, shuffle, path))
                    if nev["hits"]:
                        ordered, _ = naive_ordered(specs, list(nev["hits"]),
                                                   nev["top_priority"])
                        self.assertEqual(ordered_hits(result), ordered,
                                         (trial, shuffle, path))
                    sig_parts.append((path, result.status, tuple(ordered_hits(result))))
                seen_signatures.add(tuple(sig_parts))
            # 6 次乱序结果完全一致
            self.assertEqual(len(seen_signatures), 1, trial)


if __name__ == "__main__":
    unittest.main(verbosity=2)
