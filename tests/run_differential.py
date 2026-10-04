"""run_differential.py -- 乱序加载对拍并写 reports/differential_check.txt。

对 data/differential_fixture.json 中的 7 条规则枚举全部 7! = 5040 种加载
排列，对 13 条查询逐条比对 trie Router 与独立朴素实现（naive_matcher）的
命中集合、最高优先级与完整命中序列，全部一致才通过。
"""

from __future__ import annotations

import itertools
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

from router import Rule, Router  # noqa: E402
from naive_matcher import NaiveRouter, NaiveRule  # noqa: E402


def main():
    with open(os.path.join(ROOT, "data", "differential_fixture.json"),
              encoding="utf-8") as fh:
        fixture = json.load(fh)
    base = [Rule(r["pattern"], r["priority"], r["id"]) for r in fixture["rules"]]
    naive = NaiveRouter(
        [NaiveRule(r["pattern"], r["priority"], r["id"]) for r in fixture["rules"]]
    )
    queries = [q["path"] for q in fixture["queries"]]

    reference_signatures = None
    n_perm = 0
    n_checks = 0
    mismatches = []
    for perm in itertools.permutations(base):
        n_perm += 1
        router = Router(list(perm))
        signatures = []
        for expected in fixture["queries"]:
            path = expected["path"]
            result = router.evaluate(path)
            got = tuple(h.rule_id for h in result.matches)
            nev = naive.evaluate(path)
            n_checks += 1
            if set(got) != set(nev["hits"]) or result.top_priority != nev["top_priority"]:
                mismatches.append((n_perm, path))
            if got != tuple(expected["hits_ordered"]) or result.status != expected["status"]:
                mismatches.append((n_perm, path))
            signatures.append((path, result.status, got))
        signatures = tuple(signatures)
        if reference_signatures is None:
            reference_signatures = signatures
        elif signatures != reference_signatures:
            mismatches.append((n_perm, "<order-invariance>"))

    lines = [
        "乱序加载对拍报告（tests/run_differential.py 生成）",
        "规则数: %d | 加载排列数: %d | 查询数: %d | 比对次数: %d"
        % (len(base), n_perm, len(queries), n_checks),
        "比对维度: 命中集合(trie vs 朴素实现) + 最高优先级 + 完整命中序列(vs 夹具) + 跨排列一致性",
        "不一致项: %d" % len(mismatches),
        "结论: %s" % ("通过——加载顺序不影响匹配结果" if not mismatches else "失败"),
    ]
    if mismatches:
        lines.append("明细: %r" % (mismatches[:20],))
    text = "\n".join(lines) + "\n"
    path = os.path.join(ROOT, "reports", "differential_check.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    if mismatches:
        sys.exit(1)


if __name__ == "__main__":
    main()
