"""递归查询库：深度受限遍历 + 环检测（有向图，标准库实现）。

核心能力
--------
- ``recursive_query``：模拟递归 CTE（WITH RECURSIVE）的 DFS 遍历。
  深度限制与环检测**同时生效**，命中时返回形态不同的结果
  （``Truncation`` vs ``Cycle``），两者可在同一条路径上同时出现。
- ``find_all_cycles``：Johnson 算法枚举图中**全部基本环**，
  每个环给出节点序列，重复/旋转同构的环只报一次。
- 重复边在建图阶段去重；自环作为长度为 1 的基本环处理。
- 全程迭代式 DFS，无 Python 递归深度问题（深度 1 万也安全）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Graph",
    "Edge",
    "Cycle",
    "Truncation",
    "QueryResult",
    "recursive_query",
    "find_all_cycles",
]

Edge = Tuple[Any, Any]


def _key(node: Any) -> Any:
    """排序键：允许 int/str 等混合类型时仍有确定顺序。"""
    return (type(node).__name__, node)


@dataclass(frozen=True)
class Cycle:
    """检测到的一个环。

    ``nodes`` 为环上的全部节点，按遍历方向排列，
    末节点 -> 首节点构成闭包边（自环时 ``len(nodes) == 1``）。
    """

    nodes: Tuple[Any, ...]
    via: Tuple[Any, Any]  # 实际触发检测的回边 (u, v)

    @staticmethod
    def canonical_key(nodes: Sequence[Any]) -> Tuple[Any, ...]:
        """旋转规范化：同一组环的不同旋转写法得到同一个键。"""
        n = len(nodes)
        best = 0
        for i in range(1, n):
            if _key(nodes[i]) < _key(nodes[best]):
                best = i
        return tuple(nodes[best:] + nodes[:best])

    @property
    def key(self) -> Tuple[Any, ...]:
        return self.canonical_key(self.nodes)


@dataclass(frozen=True)
class Truncation:
    """深度限制触发点：路径已达 ``max_depth``，其后继被裁剪。"""

    node: Any
    at_depth: int


@dataclass
class QueryResult:
    """``recursive_query`` 的结果（两种触发形态明确区分）。

    ``rows`` 中普通元素为节点，环元素为 :class:`Cycle`，
    深度裁剪元素为 :class:`Truncation`。三者类型不同，可直接判别。
    """

    rows: List[Any] = field(default_factory=list)
    cycles: List[Cycle] = field(default_factory=list)
    truncated: List[Truncation] = field(default_factory=list)
    duplicates_skipped: int = 0
    unreachable: bool = False  # 起点没有任何可达边时为 True

    @property
    def hit_cycle(self) -> bool:
        return bool(self.cycles)

    @property
    def hit_depth_limit(self) -> bool:
        return bool(self.truncated)

    @property
    def shape(self) -> str:
        """结果形态：ok / depth_limit / cycle / cycle+depth_limit。"""
        if self.cycles and self.truncated:
            return "cycle+depth_limit"
        if self.cycles:
            return "cycle"
        if self.truncated:
            return "depth_limit"
        return "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shape": self.shape,
            "rows": [
                r.nodes if isinstance(r, Cycle)
                else {"truncated_at": r.node, "depth": r.at_depth}
                if isinstance(r, Truncation) else r
                for r in self.rows
            ],
            "cycles": [list(c.nodes) for c in self.cycles],
            "truncated": [
                {"node": t.node, "depth": t.at_depth} for t in self.truncated
            ],
            "duplicates_skipped": self.duplicates_skipped,
            "unreachable": self.unreachable,
        }


class Graph:
    """有向图：邻接表，建图时去重重复边（保留重复计数）。"""

    def __init__(
        self,
        edges: Iterable[Edge] = (),
        *,
        track_duplicates: bool = True,
        auto_freeze: bool = True,
    ) -> None:
        self.adj: Dict[Any, List[Any]] = {}
        self._seen: Optional[Dict[Any, set]] = {} if track_duplicates else None
        self.duplicates_skipped: int = 0
        self.frozen: bool = False
        for u, v in edges:
            self.add_edge(u, v)
        if auto_freeze:
            self.freeze()

    def add_edge(self, u: Any, v: Any) -> bool:
        if self.frozen:
            raise RuntimeError("graph is frozen")
        if u not in self.adj:
            self.adj[u] = []
        if self._seen is not None and u not in self._seen:
            self._seen[u] = set()
        seen = self._seen[u] if self._seen is not None else None
        if seen is not None:
            if v in seen:
                self.duplicates_skipped += 1
                return False
            seen.add(v)
        else:
            if v in self.adj[u]:
                self.duplicates_skipped += 1
                return False
        self.adj[u].append(v)
        self.adj.setdefault(v, [])
        return True

    def freeze(self) -> "Graph":
        """排序邻接表，丢弃去重集合，保证遍历顺序确定。"""
        if self.frozen:
            return self
        for u in self.adj:
            self.adj[u].sort(key=_key)
        self._seen = None
        self.frozen = True
        return self

    def nodes(self) -> List[Any]:
        return sorted(self.adj.keys(), key=_key)

    @property
    def num_edges(self) -> int:
        return sum(len(v) for v in self.adj.values())

    def copy_structure(self) -> "Graph":
        g = Graph.__new__(Graph)
        g.adj = {u: list(vs) for u, vs in self.adj.items()}
        g._seen = None
        g.duplicates_skipped = 0
        g.frozen = True
        return g


def recursive_query(
    graph: Graph,
    roots: Any,
    *,
    max_depth: Optional[int] = None,
) -> QueryResult:
    """从 ``roots`` 做迭代式 DFS 的“递归查询”。

    参数
    ----
    graph:
        已建图（重复边已去重）。
    roots:
        单个起始节点，或起始节点序列（多根递归 CTE）。
    max_depth:
        最大允许边数（根在深度 0）。``None`` 表示不限；
        到达该深度的节点不再扩展，记一条 :class:`Truncation`。

    环检测
    ------
    用“当前 DFS 路径栈 + on_path 位置表”判定：后继出现在当前路径上
    即为回边，截取出环上全部节点生成 :class:`Cycle`，按规范化键去重。
    自环（u -> u）同样走此路径，环长为 1。环与深度限制互不抑制：
    若回边目标深度本应超过 ``max_depth``，仍报环（环优先识别），
    而被裁节点的其它后继照常产生 :class:`Truncation`。
    """
    if not graph.frozen:
        graph.freeze()
    adj = graph.adj
    if isinstance(roots, (list, tuple, set, frozenset)):
        root_list = list(roots)
    else:
        root_list = [roots]

    result = QueryResult(duplicates_skipped=graph.duplicates_skipped)
    # (node, depth, next_sibling_index)
    stack: List[Tuple[Any, int, int]] = []
    on_path: Dict[Any, int] = {}
    path_nodes: List[Any] = []
    visited: set = set()
    reached_any_edge = False

    def push(node: Any, depth: int) -> None:
        stack.append((node, depth, 0))
        on_path[node] = depth
        path_nodes.append(node)
        visited.add(node)
        result.rows.append(node)

    for root in root_list:
        if root in visited:
            continue
        push(root, 0)

        while stack:
            u, depth, idx = stack[-1]
            neighbors = adj.get(u, ())

            if idx < len(neighbors):
                v = neighbors[idx]
                stack[-1] = (u, depth, idx + 1)
                reached_any_edge = True  # 存在从可达点出发的边（含回边/被裁剪边）

                if v in on_path:
                    # 回边：v..u + 闭包边 u->v，即环上全部节点
                    start = on_path[v]
                    cyc = tuple(path_nodes[start:])
                    result.cycles.append(Cycle(cyc, (u, v)))
                    result.rows.append(Cycle(cyc, (u, v)))
                    continue

                child_depth = depth + 1
                if max_depth is not None and child_depth > max_depth:
                    t = Truncation(v, child_depth)
                    result.truncated.append(t)
                    result.rows.append(t)
                    continue

                reached_any_edge = True
                if v in visited:
                    # 指向已完结子树：无环；递归 CTE 的 UNION 语义下不重复发射
                    continue
                push(v, child_depth)
            else:
                stack.pop()
                del on_path[u]
                path_nodes.pop()

    # 起点（任一根）不存在任何可达出边时为 True
    result.unreachable = not reached_any_edge
    # 环按规范化键去重，保持首次发现顺序（顺序本身由排序邻接表确定）
    deduped: List[Cycle] = []
    seen_keys: set = set()
    for c in result.cycles:
        if c.key not in seen_keys:
            seen_keys.add(c.key)
            deduped.append(c)
    result.cycles = deduped
    return result


# --------------------------------------------------------------------------
# 全量基本环枚举：Johnson 算法（确定性版本）
# --------------------------------------------------------------------------

def _tarjan_sccs(adj: Dict[Any, List[Any]]) -> List[List[Any]]:
    index = 0
    indices: Dict[Any, int] = {}
    low: Dict[Any, int] = {}
    stack: List[Any] = []
    on: set = set()
    sccs: List[List[Any]] = []

    def strong(v: Any) -> None:
        nonlocal index
        indices[v] = low[v] = index
        index += 1
        stack.append(v)
        on.add(v)
        for w in adj.get(v, ()):  # 邻接表已排序
            if w not in indices:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], indices[w])
        if low[v] == indices[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    for v in sorted(adj.keys(), key=_key):
        if v not in indices:
            strong(v)
    return sccs


def find_all_cycles(graph: Graph) -> List[Tuple[Any, ...]]:
    """枚举图中全部基本环（Johnson），返回去重后的环节点序列列表。

    每个序列按边方向排列，末节点连回首节点；自环为单元素序列。
    结果按环起点规范化后字典序排列，跨运行完全确定。
    """
    if not graph.frozen:
        graph.freeze()
    adj = graph.adj
    sys.setrecursionlimit(max(1_000_000, sys.getrecursionlimit()))

    found: Dict[Tuple[Any, ...], Tuple[Any, ...]] = {}

    def remember(path: List[Any]) -> None:
        k = Cycle.canonical_key(path)
        found.setdefault(k, tuple(path))

    def circuits(
        sub: Dict[Any, set],
        start: Any,
    ) -> None:
        blocked: set = {start}
        b: Dict[Any, set] = {v: set() for v in sub}
        st: List[Any] = [start]

        def unblock(u: Any) -> None:
            blocked.discard(u)
            while b[u]:
                w = b[u].pop()
                if w in blocked:
                    unblock(w)

        def circuit(v: Any) -> bool:
            found_cycle = False
            for w in sorted(sub.get(v, ()), key=_key):
                if w == start:
                    remember(st)
                    found_cycle = True
                elif w not in blocked:
                    st.append(w)
                    blocked.add(w)
                    if circuit(w):
                        found_cycle = True
                    st.pop()
            if found_cycle:
                unblock(v)
            else:
                for w in sub.get(v, ()):
                    b[w].add(v)
            return found_cycle

        circuit(start)

    # 基本环不会跨越强连通分量：先一次性求全部 SCC，
    # 再对每个非平凡分量（含自环的单点分量）跑 Johnson circuit。
    # 避免朴素实现中每轮全图重算 SCC 的 O(n * (V+E)) 退化。
    for comp in _tarjan_sccs(adj):
        if len(comp) == 1:
            only = comp[0]
            if only in adj.get(only, ()):  # 自环
                remember([only])
            continue
        comp_set = set(comp)
        sub = {
            v: {w for w in adj[v] if w in comp_set}
            for v in comp
        }
        for s in sorted(comp, key=_key):
            circuits(sub, s)

    # 直接返回规范化旋转（从最小节点起），保证输出与遍历顺序无关
    return sorted(found.keys(), key=lambda t: [_key(x) for x in t])
