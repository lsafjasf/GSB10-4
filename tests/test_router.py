import json
from pathlib import Path
import random
import unittest

from router import MatchResult, RouteConflictError, RouteMatcher, Rule


class RouteMatcherTest(unittest.TestCase):
    def test_empty_rule_set(self):
        result = RouteMatcher().match("/anything")
        self.assertFalse(result.ok)
        self.assertIsNone(result.rule)
        self.assertEqual(result.matches, ())

    def test_single_exact_rule(self):
        matcher = RouteMatcher([Rule("exact", "/api/users", 0)])
        result = matcher.match("/api/users")
        self.assertTrue(result.ok)
        self.assertEqual(result.rule.id, "exact")
        self.assertFalse(matcher.match("/api/users/1").ok)

    def test_explicit_priority_beats_longer_prefix(self):
        matcher = RouteMatcher(
            [
                Rule("long-low", "/api/users", 1),
                Rule("short-high", "/api/*", 10),
            ]
        )
        result = matcher.match("/api/users")
        self.assertEqual(result.rule.id, "short-high")
        self.assertEqual(result.priority, 10)

    def test_same_priority_conflict_lists_every_match(self):
        matcher = RouteMatcher(
            [
                Rule("z-last-loaded", "/api/*", 10),
                Rule("a-exact", "/api/users", 10),
                Rule("m-middle", "/api/u*rs", 10),
                Rule("other", "/other/*", 10),
            ]
        )
        with self.assertRaises(RouteConflictError) as caught:
            matcher.match("/api/users")
        conflict = caught.exception
        self.assertEqual(
            [rule.id for rule in conflict.conflicts[10]],
            ["a-exact", "m-middle", "z-last-loaded"],
        )
        message = str(conflict)
        for rule_id in ("a-exact", "m-middle", "z-last-loaded"):
            self.assertIn(rule_id, message)

    def test_same_priority_non_overlapping_rules_do_not_conflict(self):
        matcher = RouteMatcher(
            [
                Rule("users", "/users/*", 5),
                Rule("orders", "/orders/*", 5),
            ]
        )
        self.assertEqual(matcher.match("/users/1").rule.id, "users")
        self.assertEqual(matcher.match("/orders/1").rule.id, "orders")

    def test_same_pattern_same_priority_conflicts(self):
        matcher = RouteMatcher(
            [Rule("first", "/same", 3), Rule("second", "/same", 3)]
        )
        with self.assertRaises(RouteConflictError) as caught:
            matcher.match("/same")
        self.assertEqual(
            [rule.id for rule in caught.exception.conflicts[3]],
            ["first", "second"],
        )

    def test_same_pattern_different_priority_is_not_a_conflict(self):
        matcher = RouteMatcher(
            [Rule("low", "/same", 1), Rule("high", "/same", 2)]
        )
        self.assertEqual(matcher.match("/same").rule.id, "high")

    def test_wildcard_matches_across_segments_and_empty_tail(self):
        matcher = RouteMatcher(
            [
                Rule("static", "/static/*", 1),
                Rule("middle", "/api/*/users", 2),
                Rule("all", "*", 0),
            ]
        )
        self.assertEqual(matcher.match("/static/a/b.css").rule.id, "static")
        self.assertEqual(matcher.match("/static/").rule.id, "static")
        self.assertEqual(matcher.match("/api/v1/users").rule.id, "middle")
        self.assertEqual(matcher.match("/unmatched").rule.id, "all")

    def test_escaped_star_and_backslash_are_literal(self):
        matcher = RouteMatcher(
            [
                Rule("literal-star", r"/files/\*", 10),
                Rule("literal-backslash", r"/files/\\", 10),
                Rule("trailing-backslash", "/files\\", 10),
                Rule("ordinary-backslash", r"/a\q", 10),
                Rule("wild", "/files/*", 1),
            ]
        )
        self.assertEqual(matcher.match("/files/*").rule.id, "literal-star")
        self.assertEqual(matcher.match("/files/\\").rule.id, "literal-backslash")
        self.assertEqual(matcher.match("/files\\").rule.id, "trailing-backslash")
        self.assertEqual(matcher.match(r"/a\q").rule.id, "ordinary-backslash")
        self.assertEqual(matcher.match("/files/readme").rule.id, "wild")

    def test_escaped_wildcard_does_not_become_wildcard_at_same_priority(self):
        matcher = RouteMatcher(
            [
                Rule("literal", r"/literal/\*", 7),
                Rule("prefix", "/literal/*", 1),
            ]
        )
        self.assertEqual(matcher.match("/literal/*").rule.id, "literal")
        self.assertEqual(matcher.match("/literal/other").rule.id, "prefix")

    def test_escaped_and_real_wildcard_can_be_mixed(self):
        matcher = RouteMatcher(
            [
                Rule("mixed", r"/files/\*/*", 10),
                Rule("fallback", "/files/*", 1),
            ]
        )
        self.assertEqual(matcher.match("/files/*/asset").rule.id, "mixed")
        self.assertEqual(matcher.match("/files/readme/asset").rule.id, "fallback")

    def test_load_order_does_not_change_results(self):
        rules = [
            Rule("root", "*", -10),
            Rule("api", "/api/*", 10),
            Rule("users", "/api/users/*", 10),
            Rule("health", "/health", 20),
            Rule("escaped", r"/literal/\*", 30),
            Rule("exact-low", "/api/users/exact", 1),
        ]
        expected = {
            "/health": ("ok", "health"),
            "/api/users/1": ("conflict", ("api", "users")),
            "/literal/*": ("ok", "escaped"),
            "/api/users/exact": ("conflict", ("api", "users")),
            "/other": ("ok", "root"),
        }
        for seed in range(20):
            shuffled = list(rules)
            random.Random(seed).shuffle(shuffled)
            matcher = RouteMatcher(shuffled)
            for path, (status, expected_ids) in expected.items():
                if status == "ok":
                    self.assertEqual(matcher.match(path).rule.id, expected_ids)
                else:
                    with self.assertRaises(RouteConflictError) as caught:
                        matcher.match(path)
                    self.assertEqual(
                        tuple(
                            rule.id
                            for priority in sorted(
                                caught.exception.conflicts, reverse=True
                            )
                            for rule in caught.exception.conflicts[priority]
                        ),
                        expected_ids,
                    )

    def test_shadowed_lower_priority_conflict_is_still_reported(self):
        matcher = RouteMatcher(
            [
                Rule("high", "/api/*", 100),
                Rule("low-a", "/api/users/*", 1),
                Rule("low-b", "/api/*/1", 1),
            ]
        )
        with self.assertRaises(RouteConflictError) as caught:
            matcher.match("/api/users/1")
        with self.assertRaises(RouteConflictError) as audit_caught:
            matcher.match("/api/users/1", audit=True)
        self.assertEqual(
            caught.exception.conflicts,
            audit_caught.exception.conflicts,
        )
        self.assertEqual(
            [rule.id for rule in caught.exception.conflicts[1]],
            ["low-a", "low-b"],
        )

    def test_audit_returns_all_shadowed_matches_without_conflict(self):
        matcher = RouteMatcher(
            [
                Rule("high", "/api/*", 100),
                Rule("low", "/api/users/*", 1),
            ]
        )
        normal = matcher.match("/api/users/1")
        audit = matcher.match("/api/users/1", audit=True)
        self.assertEqual([rule.id for rule in normal.matches], ["high"])
        self.assertEqual([rule.id for rule in audit.matches], ["high", "low"])

    def test_pairing_data_load_orders(self):
        data_path = Path(__file__).resolve().parents[1] / "data" / "shuffled_pairing.json"
        payload = json.loads(data_path.read_text(encoding="utf-8"))
        for load_order in payload["load_orders"]:
            matcher = RouteMatcher(
                Rule(
                    id=item["id"],
                    pattern=item["pattern"],
                    priority=item["priority"],
                )
                for item in load_order
            )
            for path, expected in payload["expected"].items():
                if expected["status"] == "ok":
                    result = matcher.match(path)
                    self.assertEqual(result.rule.id, expected["rule"])
                    self.assertEqual(result.priority, expected["priority"])
                else:
                    with self.assertRaises(RouteConflictError) as caught:
                        matcher.match(path)
                    actual = {
                        str(priority): [rule.id for rule in rules]
                        for priority, rules in caught.exception.conflicts.items()
                    }
                    self.assertEqual(actual, expected["conflicts"])

    def test_duplicate_rule_id_is_rejected(self):
        matcher = RouteMatcher([Rule("same", "/a", 1)])
        with self.assertRaises(ValueError):
            matcher.add_rule(Rule("same", "/b", 2))

    def test_result_type(self):
        result = RouteMatcher().match("/x")
        self.assertIsInstance(result, MatchResult)


if __name__ == "__main__":
    unittest.main()
