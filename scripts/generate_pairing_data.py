#!/usr/bin/env python3
"""Generate the checked-in shuffled-load pairing fixture."""

import json
from pathlib import Path
import random


RULES = [
    {"id": "r-root", "pattern": "*", "priority": -10},
    {"id": "r-api", "pattern": "/api/*", "priority": 10},
    {"id": "r-users", "pattern": "/api/users/*", "priority": 10},
    {"id": "r-exact-low", "pattern": "/api/users/exact", "priority": 1},
    {"id": "r-health", "pattern": "/health", "priority": 20},
    {"id": "r-literal-star", "pattern": "/literal/\\*", "priority": 30},
    {"id": "r-wild-low", "pattern": "/literal/*", "priority": 5},
    {"id": "r-order", "pattern": "/orders/*", "priority": 10},
    {"id": "r-middle", "pattern": "/api/*/items", "priority": 15},
]

EXPECTED = {
    "/health": {"status": "ok", "rule": "r-health", "priority": 20},
    "/api/users/42": {
        "status": "conflict",
        "conflicts": {"10": ["r-api", "r-users"]},
    },
    "/api/users/exact": {
        "status": "conflict",
        "conflicts": {"10": ["r-api", "r-users"]},
    },
    "/api/v1/items": {"status": "ok", "rule": "r-middle", "priority": 15},
    "/literal/*": {"status": "ok", "rule": "r-literal-star", "priority": 30},
    "/literal/abc": {"status": "ok", "rule": "r-wild-low", "priority": 5},
    "/orders/9": {"status": "ok", "rule": "r-order", "priority": 10},
    "/none": {"status": "ok", "rule": "r-root", "priority": -10},
    "": {"status": "ok", "rule": "r-root", "priority": -10},
}


def shuffled_rules(seed):
    rules = list(RULES)
    random.Random(seed).shuffle(rules)
    return rules


def main():
    payload = {
        "description": (
            "Same rule set in two deterministic shuffled load orders; "
            "every expected match/conflict must be identical."
        ),
        "load_orders": [shuffled_rules(20261005), shuffled_rules(99173)],
        "expected": EXPECTED,
    }
    output = Path(__file__).resolve().parents[1] / "data" / "shuffled_pairing.json"
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
