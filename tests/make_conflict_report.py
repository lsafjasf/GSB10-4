"""make_conflict_report.py -- 生成 reports/conflict_report.txt（确定性）。

场景：
A. 同优先级两条命中（前缀包含关系）-> 冲突，列出全部命中项
B. 转义字面星号与 glob 同优先级命中同一请求 -> 冲突
C. 同优先级三条命中 -> 冲突，全部列出
D. 显式优先级压过最长前缀 -> 正常命中（对照组，无冲突）
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from router import Rule, Router  # noqa: E402


def main():
    with open(os.path.join(ROOT, "data", "differential_fixture.json"),
              encoding="utf-8") as fh:
        fixture = json.load(fh)
    fixture_router = Router(
        [Rule(r["pattern"], r["priority"], r["id"]) for r in fixture["rules"]]
    )

    router_c = Router([
        Rule("/a", priority=2, id="a"),
        Rule("/a/b", priority=2, id="ab"),
        Rule("/a/b/c", priority=2, id="abc"),
    ])
    router_d = Router([
        Rule("/a/b/c", priority=0, id="long-but-low"),
        Rule("/a", priority=10, id="short-but-high"),
    ])

    sections = [
        ("场景 A：同优先级两条命中（/api 与 /api/users/u1 同为 priority=1）",
         fixture_router.evaluate("/api/users/u1")),
        ("场景 B：转义字面星号 /files/a\\*b 与 glob /files/* 同优先级命中 /files/a*b",
         fixture_router.evaluate("/files/a*b")),
        ("场景 C：同优先级三条命中（/a、/a/b、/a/b/c 同为 priority=2）",
         router_c.evaluate("/a/b/c")),
        ("场景 D（对照）：显式优先级压过最长前缀，无冲突",
         router_d.evaluate("/a/b/c")),
    ]

    out = ["路由冲突报告（由 tests/make_conflict_report.py 生成，内容确定性）", ""]
    for title, result in sections:
        out.append("=" * 60)
        out.append(title)
        out.append("-" * 60)
        out.append(result.report())
        out.append("")
    text = "\n".join(out)
    path = os.path.join(ROOT, "reports", "conflict_report.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print("wrote", path)
    print(text)


if __name__ == "__main__":
    main()
