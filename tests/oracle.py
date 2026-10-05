"""对拍用暴力 oracle：与库实现完全独立的环枚举。

思路：对每个起点 s，DFS 枚举所有“简单路径”（不重复节点），
一旦走到 s 就记录一个环；用“路径上节点 >= s”剪枝保证每个
基本环恰好生成一次。指数级复杂度，仅用于小图对拍。
"""

from typing import Any, Dict, Hashable, List, Set, Tuple


def _key(node: Any) -> Any:
    return (type(node).__name__, node)


def oracle_cycles(edges: List[Tuple[Any, Any]]) -> List[Tuple[Any, ...]]:
    adj: Dict[Any, List[Any]] = {}
    nodes: Set[Any] = set()
    for u, v in edges:
        adj.setdefault(u, [])
        if v not in adj[u]:
            adj[u].append(v)
        nodes.add(u)
        nodes.add(v)
    for u in adj:
        adj[u].sort(key=_key)

    def canonical(path: List[Any]) -> Tuple[Any, ...]:
        n = len(path)
        best = 0
        for i in range(1, n):
            if _key(path[i]) < _key(path[best]):
                best = i
        return tuple(path[best:] + path[:best])

    found: Set[Tuple[Any, ...]] = set()

    def dfs(start: Any, cur: Any, path: List[Any], in_path: Set[Any]) -> None:
        for w in adj.get(cur, ()):
            if w == start:
                found.add(canonical(path))
            elif w not in in_path and _key(w) >= _key(start):
                in_path.add(w)
                path.append(w)
                dfs(start, w, path, in_path)
                path.pop()
                in_path.discard(w)

    for s in sorted(nodes, key=_key):
        dfs(s, s, [s], {s})

    return sorted(found, key=lambda t: [_key(x) for x in t])
