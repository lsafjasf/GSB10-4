"""演示：同一张图上深度限制与环检测同时生效时的不同结果形态。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cycle_query import Graph, find_all_cycles, recursive_query

# 8 字形双环相交 + 自环 + 重复边 + 不可达环
EDGES = [
    (0, 1), (1, 2), (2, 3), (3, 0),   # 环 A: 0-1-2-3-0
    (2, 4), (4, 5), (5, 2),           # 环 B: 2-4-5-2（与 A 共用节点 2）
    (5, 5),                           # 自环
    (1, 2),                           # 重复边
    (6, 7), (7, 6),                   # 从根 0 不可达的环
]


def show(title, res):
    print("== %s ==" % title)
    print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    print()


def main():
    g = Graph(EDGES)
    show("不限深（shape=cycle）", recursive_query(g, 0))
    show("max_depth=2（shape=depth_limit）", recursive_query(g, 0, max_depth=2))
    show("max_depth=3（shape=cycle+depth_limit）",
         recursive_query(g, 0, max_depth=3))
    show("不存在的根（unreachable）", recursive_query(g, 9))
    print("== 全量基本环（含不可达分量） ==")
    for c in find_all_cycles(g):
        print(" ", " -> ".join(map(str, c)), "->", c[0])


if __name__ == "__main__":
    main()
