#!/usr/bin/env python3
"""Benchmark and large-scale shuffled-load check for the route matcher."""

from __future__ import annotations

import argparse
import gc
import json
import platform
import random
import sys
import time

from router import RouteConflictError, RouteMatcher, Rule


def build_rules(rule_count: int, seed: int) -> list[Rule]:
    exact_count = rule_count * 9 // 10
    rules: list[Rule] = []

    for index in range(exact_count):
        rules.append(
            Rule(
                id=f"exact-{index:05d}",
                pattern=f"/r/{index:05d}",
                priority=index % 10,
            )
        )

    wildcard_count = rule_count - exact_count
    for index in range(wildcard_count):
        rules.append(
            Rule(
                id=f"wild-{index:05d}",
                pattern=f"/w/{index:05d}/*",
                priority=10 + index % 10,
            )
        )

    random.Random(seed).shuffle(rules)
    return rules


def build_queries(query_count: int, exact_count: int, wildcard_count: int):
    rng = random.Random(20261005)
    queries = []
    for serial in range(query_count):
        choice = rng.random()
        if choice < 0.90:
            index = rng.randrange(exact_count)
            queries.append(
                (
                    f"/r/{index:05d}",
                    ("ok", f"exact-{index:05d}", index % 10),
                )
            )
        elif choice < 0.95:
            index = rng.randrange(wildcard_count)
            suffix = rng.randrange(query_count)
            queries.append(
                (
                    f"/w/{index:05d}/asset/{serial}-{suffix}",
                    ("ok", f"wild-{index:05d}", 10 + index % 10),
                )
            )
        else:
            queries.append((f"/missing/{serial}", ("no_match", None, None)))
    return queries


def run_queries(matcher: RouteMatcher, queries):
    results = []
    matched = 0
    no_match = 0
    conflicts = 0

    for path, _expected in queries:
        try:
            result = matcher.match(path)
        except RouteConflictError:
            conflicts += 1
            results.append(("conflict", None, None))
        else:
            if result.ok:
                matched += 1
                results.append(("ok", result.rule.id, result.priority))
            else:
                no_match += 1
                results.append(("no_match", None, None))
    return results, {"matched": matched, "no_match": no_match, "conflicts": conflicts}


def run(rule_count: int, query_count: int) -> dict:
    exact_count = rule_count * 9 // 10
    wildcard_count = rule_count - exact_count
    queries = build_queries(query_count, exact_count, wildcard_count)

    gc.collect()
    build_start = time.perf_counter()
    matcher = RouteMatcher(build_rules(rule_count, seed=12345))
    build_seconds = time.perf_counter() - build_start

    memory_start = time.perf_counter()
    stats = matcher.stats()
    memory_seconds = time.perf_counter() - memory_start

    match_start = time.perf_counter()
    results, result_counts = run_queries(matcher, queries)
    matching_seconds = time.perf_counter() - match_start

    expected_results = tuple(expected for _path, expected in queries)
    first_order_mismatches = sum(
        actual != expected
        for actual, expected in zip(results, expected_results)
    )

    del matcher, results
    gc.collect()
    reordered = RouteMatcher(build_rules(rule_count, seed=67890))
    reordered_results, _ = run_queries(reordered, queries)
    order_mismatches = sum(
        actual != expected
        for actual, expected in zip(reordered_results, expected_results)
    )

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "rules": rule_count,
        "exact_rules": exact_count,
        "wildcard_rules": wildcard_count,
        "queries": query_count,
        "priorities": 20,
        "build_seconds": build_seconds,
        "matching_seconds": matching_seconds,
        "throughput_queries_per_second": query_count / matching_seconds,
        "memory_walk_seconds": memory_seconds,
        "trie_nodes": stats["trie_nodes"],
        "index_bytes": stats["index_bytes"],
        "index_mib": stats["index_bytes"] / 1024 / 1024,
        "result_counts": result_counts,
        "expected_result_mismatches_first_load": first_order_mismatches,
        "shuffled_load_seeds": [12345, 67890],
        "order_invariance_queries": query_count,
        "order_invariance_mismatches": order_mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules", type=int, default=50_000)
    parser.add_argument("--queries", type=int, default=100_000)
    parser.add_argument("--output")
    args = parser.parse_args()

    report = run(args.rules, args.queries)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as output:
            output.write(rendered + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
