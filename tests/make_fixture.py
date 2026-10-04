"""make_fixture.py -- 生成对拍夹具 data/differential_fixture.json。

期望结果完全由独立计算得到（不 import src/router.py）：
* 命中集合与最高优先级由 tests/naive_matcher.py 线性扫描得出；
* canonical 输出顺序用独立的 specificity 计算（段数/字面段数/字面字符数）。
随后 tests/test_router.py 让 trie Router 加载全部 N! 种规则排列并逐查询对拍。
"""

from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from naive_matcher import NaiveRouter, NaiveRule, _split  # noqa: E402

RULES = [
    {"id": "api",      "pattern": "/api",          "priority": 1},
    {"id": "deep",     "pattern": "/api/users/u1", "priority": 1},
    {"id": "apiwild",  "pattern": "/api/v*",       "priority": 3},
    {"id": "admin",    "pattern": "/admin",        "priority": 5},
    {"id": "root",     "pattern": "/",             "priority": 0},
    {"id": "escstar",  "pattern": "/files/a\\*b",  "priority": 2},
    {"id": "filesglob", "pattern": "/files/*",     "priority": 2},
]

QUERIES = [
    "/",
    "/other",
    "/admin",
    "/api",
    "/api/users/u1",
    "/api/v2",
    "/api/v10/status",
    "/files/a*b",
    "/files/cd",
    "/files/",
    "/admin/users",
    "/adminx",
    "/api/v",
]


def _literal_len(seg: str) -> int:
    """独立计算转义后的字面字符数（与 router.py 的实现分开写）。"""
    total = 0
    i = 0
    while i < len(seg):
        if seg[i] == "\\" and i + 1 < len(seg) and seg[i + 1] in ("*", "\\"):
            total += 1
            i += 2
        else:
            total += 1
            i += 1
    return total


def _is_literal_seg(seg: str) -> bool:
    i = 0
    while i < len(seg):
        if seg[i] == "\\" and i + 1 < len(seg) and seg[i + 1] in ("*", "\\"):
            i += 2
        elif seg[i] == "*":
            return False
        else:
            i += 1
    return True


def specificity(pattern: str):
    segs = _split(pattern)
    lit_segs = [s for s in segs if _is_literal_seg(s)]
    return (len(segs), len(lit_segs), sum(_literal_len(s) for s in segs))


def main():
    by_id = {r["id"]: r for r in RULES}
    naive = NaiveRouter(
        [NaiveRule(raw=r["pattern"], priority=r["priority"], rule_id=r["id"]) for r in RULES]
    )

    expected_queries = []
    for path in QUERIES:
        ev = naive.evaluate(path)
        hit_ids = ev["hits"]
        top = ev["top_priority"]
        if not hit_ids:
            status, winner, ordered = "no_match", None, []
        else:
            top_group = [i for i in hit_ids if by_id[i]["priority"] == top]
            ordered = sorted(
                hit_ids,
                key=lambda i: (-by_id[i]["priority"],
                               tuple(-x for x in specificity(by_id[i]["pattern"])),
                               i),
            )
            if len(top_group) == 1:
                status, winner = "match", top_group[0]
            else:
                status, winner = "conflict", None
        expected_queries.append({
            "path": path,
            "status": status,
            "winner": winner,
            "top_priority": top,
            "hits_ordered": ordered,
            "conflicting": sorted(i for i in hit_ids
                                  if top is not None and by_id[i]["priority"] == top)
                           if status == "conflict" else [],
        })

    fixture = {
        "description": "路由优先级对拍夹具：trie Router 在全部规则排列下结果必须与此一致",
        "matching_semantics": {
            "match_type": "prefix",
            "star_glob": "段内任意字符（含空），不跨 '/'；不存在跨段通配，'*' 单独成段即匹配任意一个非空段",
            "escapes": "\\* 与 \\\\ 是字面量；其余转义与结尾孤立反斜杠为加载期错误",
            "decision": "显式 priority 大者胜（整数，默认 0）；同优先级多命中=冲突；最长前缀不参与裁决",
            "order_independence": "规则按规范化 (pattern, priority) 排序后建索引",
        },
        "rules": RULES,
        "rule_count": len(RULES),
        "permutations_tested": math.factorial(len(RULES)),
        "queries": expected_queries,
    }

    out = os.path.join(os.path.dirname(__file__), "..", "data", "differential_fixture.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("wrote", os.path.abspath(out),
          "| rules:", len(RULES),
          "| permutations:", fixture["permutations_tested"],
          "| queries:", len(QUERIES))


if __name__ == "__main__":
    main()
