"""手工分析对拍数据：每个用例附带手工推导的期望结果。

字段：
  name/desc        说明与手工分析
  edges            边列表（含重复边/自环）
  start, max_depth 查询参数
  expected_*       手工推导的期望值
"""

# --- 用例 1：无环深链 ---
# 链 1->2->...->10。max_depth=3 时：深度 0,1,2,3 到达节点 1,2,3,4；
# 节点 4 深度为 3 达到上限且有出边，被截断；无环。
CHAIN = {
    "name": "acyclic_deep_chain",
    "desc": "无环深链 1->2->...->10，max_depth=3",
    "edges": [(i, i + 1) for i in range(1, 10)],
    "start": 1,
    "max_depth": 3,
    "expected_status": "depth_limited",
    "expected_visited": {1: 0, 2: 1, 3: 2, 4: 3},
    "expected_truncated": [4],
    "expected_cycle_nodes": [],
    "expected_cycle_sequences": [],
}

# 同一链放大深度：全部可达，无截断无环。
CHAIN_FULL = {
    "name": "acyclic_chain_full_depth",
    "desc": "无环链放大深度后全部可达",
    "edges": [(i, i + 1) for i in range(1, 10)],
    "start": 1,
    "max_depth": 20,
    "expected_status": "ok",
    "expected_visited": {i: i - 1 for i in range(1, 11)},
    "expected_truncated": [],
    "expected_cycle_nodes": [],
    "expected_cycle_sequences": [],
}

# --- 用例 2：单个环 ---
# a->b->c->a 成环，c->d 是环上伸出的尾。SCC={a,b,c}；d 不在环上。
# 手工枚举基本环：a->b->c->a，唯一，序列 [a,b,c,a]。
SINGLE_CYCLE = {
    "name": "single_cycle_with_tail",
    "desc": "单环 a->b->c->a，环尾 c->d",
    "edges": [("a", "b"), ("b", "c"), ("c", "a"), ("c", "d")],
    "start": "a",
    "max_depth": 10,
    "expected_status": "cycle_detected",
    "expected_visited": {"a": 0, "b": 1, "c": 2, "d": 3},
    "expected_truncated": [],
    "expected_cycle_nodes": [["a", "b", "c"]],
    "expected_cycle_sequences": [["a", "b", "c", "a"]],
}

# --- 用例 3：多环相交 ---
# 环1: a->b->c->a；环2: c->d->e->c；两环在节点 c 相交。
# 它们同属一个 SCC={a,b,c,d,e}（环上全部节点），基本环恰为两个：
# [a,b,c,a] 与 [c,d,e,c]。
INTERSECTING_CYCLES = {
    "name": "two_cycles_intersect",
    "desc": "两环相交于 c：a-b-c-a 与 c-d-e-c",
    "edges": [
        ("a", "b"), ("b", "c"), ("c", "a"),
        ("c", "d"), ("d", "e"), ("e", "c"),
    ],
    "start": "a",
    "max_depth": 10,
    "expected_status": "cycle_detected",
    "expected_visited": {"a": 0, "b": 1, "c": 2, "d": 3, "e": 4},
    "expected_truncated": [],
    "expected_cycle_nodes": [["a", "b", "c", "d", "e"]],
    "expected_cycle_sequences": [["a", "b", "c", "a"], ["c", "d", "e", "c"]],
}

# --- 用例 4：自环 ---
# x->x 是自环，x->y 是出边。自环节点集 {x}，序列 [x,x]；y 不在环上。
SELF_LOOP = {
    "name": "self_loop",
    "desc": "自环 x->x，带出边 x->y",
    "edges": [("x", "x"), ("x", "y")],
    "start": "x",
    "max_depth": 10,
    "expected_status": "cycle_detected",
    "expected_visited": {"x": 0, "y": 1},
    "expected_truncated": [],
    "expected_cycle_nodes": [["x"]],
    "expected_cycle_sequences": [["x", "x"]],
}

# --- 用例 5：重复边 ---
# (1,2) 出现 3 次，去重后与普通链等价；duplicates_removed=2。
DUPLICATE_EDGES = {
    "name": "duplicate_edges",
    "desc": "重复边 (1,2) x3，去重后无环",
    "edges": [(1, 2), (1, 2), (1, 2), (2, 3)],
    "start": 1,
    "max_depth": 10,
    "expected_status": "ok",
    "expected_visited": {1: 0, 2: 1, 3: 2},
    "expected_truncated": [],
    "expected_cycle_nodes": [],
    "expected_cycle_sequences": [],
    "expected_duplicates": 2,
}

# --- 用例 6：无可达路径（不可达的环不算）---
# 图中存在环 p->q->p，但从孤立起点 s 不可达；查询结果必须无环、无截断。
UNREACHABLE_CYCLE = {
    "name": "unreachable_cycle",
    "desc": "起点 s 无出边，环 p-q-p 不可达",
    "edges": [("p", "q"), ("q", "p"), ("s", "t")],
    "start": "s",
    "max_depth": 10,
    "expected_status": "ok",
    "expected_visited": {"s": 0, "t": 1},
    "expected_truncated": [],
    "expected_cycle_nodes": [],
    "expected_cycle_sequences": [],
}

# --- 用例 7：深度限制与环检测同时触发 ---
# 1->2->3->2 成环（2,3），3->4->5->... 为深链。
# max_depth=2：到达深度 0/1/2 = 1/2/3；节点 3 达上限被截断；
# 深度限制内可达子图仍含环 2->3->2。
BOTH_TRIGGER = {
    "name": "depth_limit_and_cycle_both",
    "desc": "环 2-3-2 与深链 3->4->5...，max_depth=2 同时触发",
    "edges": [(1, 2), (2, 3), (3, 2)] + [(i, i + 1) for i in range(3, 30)],
    "start": 1,
    "max_depth": 2,
    "expected_status": "cycle_detected_and_depth_limited",
    "expected_visited": {1: 0, 2: 1, 3: 2},
    "expected_truncated": [3],
    "expected_cycle_nodes": [[2, 3]],
    "expected_cycle_sequences": [[2, 3, 2]],
}

# --- 用例 8：互不相交的两个环 ---
# a->b->a 与 d->e->d，从 a 可达两个环（经 a->d）。
# 两个独立 SCC，各含一个基本环。
DISJOINT_CYCLES = {
    "name": "two_disjoint_cycles",
    "desc": "两个不相交环 a-b-a、d-e-d，经 a->d 相连",
    "edges": [("a", "b"), ("b", "a"), ("a", "d"), ("d", "e"), ("e", "d")],
    "start": "a",
    "max_depth": 10,
    "expected_status": "cycle_detected",
    "expected_visited": {"a": 0, "b": 1, "d": 1, "e": 2},
    "expected_truncated": [],
    "expected_cycle_nodes": [["a", "b"], ["d", "e"]],
    "expected_cycle_sequences": [["a", "b", "a"], ["d", "e", "d"]],
}

QUERY_CASES = [
    CHAIN, CHAIN_FULL, SINGLE_CYCLE, INTERSECTING_CYCLES, SELF_LOOP,
    DUPLICATE_EDGES, UNREACHABLE_CYCLE, BOTH_TRIGGER, DISJOINT_CYCLES,
]
