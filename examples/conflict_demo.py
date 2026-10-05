#!/usr/bin/env python3
"""Print normal and audit-mode conflict reports."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router import RouteConflictError, RouteMatcher, Rule


def print_conflict(matcher, path, *, audit=False):
    try:
        result = matcher.match(path, audit=audit)
    except RouteConflictError as exc:
        print(str(exc))
        for priority in sorted(exc.conflicts, reverse=True):
            for rule in exc.conflicts[priority]:
                print(f"  priority={priority} id={rule.id} pattern={rule.pattern!r}")
    else:
        print(f"winner: {result.rule.id}")


normal = RouteMatcher(
    [
        Rule("r-api", "/api/*", 10),
        Rule("r-users", "/api/users/*", 10),
        Rule("r-exact", "/api/users/42", 10),
        Rule("r-middle", "/api/*/42", 10),
        Rule("r-lower", "/api/users/42", 1),
    ]
)
print_conflict(normal, "/api/users/42")

shadowed = RouteMatcher(
    [
        Rule("r-high", "/api/*", 100),
        Rule("r-low-a", "/api/users/*", 1),
        Rule("r-low-b", "/api/*/42", 1),
    ]
)
print_conflict(shadowed, "/api/users/42")

audit_only = RouteMatcher(
    [
        Rule("r-high", "/api/*", 100),
        Rule("r-low", "/api/users/*", 1),
    ]
)
result = audit_only.match("/api/users/42", audit=True)
print("audit matches: " + ", ".join(rule.id for rule in result.matches))
