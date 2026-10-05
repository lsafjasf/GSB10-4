"""recquery: 带环检测与深度限制的递归查询库（仅标准库）。

设计要点：
- 全迭代实现（BFS / 迭代 Tarjan），深度 1 万+ 的链不会触发 RecursionError。
- 环检测 = 在可达子图上求强连通分量（SCC）。SCC 大小 > 1，或大小为 1 且带
  自环，则该 SCC 的全部节点都在环上（"环上的全部节点"）。
- 每个环区域（SCC）内枚举基本环（elementary cycles），给出节点序列；
  枚举数量有上限（cycle_cap），防止超大 SCC 上组合爆炸。
- 深度限制与环检测同时生效：遍历被深度截断，环检测作用于"深度限制内可达"
  的子图，结果形态通过 status / truncated / cycles 字段区分。
"""

from collections import deque

STATUS_OK = "ok"
STATUS_DEPTH_LIMITED = "depth_limited"
STATUS_CYCLE = "cycle_detected"
STATUS_BOTH = "cycle_detected_and_depth_limited"


def build_graph(edges):
    """由 (u, v) 边序列构建邻接表。重复边去重。

    返回 (graph, duplicates_removed)。graph: {node: [sorted neighbors]}。
    自环 (u, u) 保留；孤立终点节点也会出现在 graph 中（邻接表为空）。
    """
    adj = {}
    duplicates = 0
    for u, v in edges:
        nbrs = adj.setdefault(u, set())
        if v in nbrs:
            duplicates += 1
        else:
            nbrs.add(v)
        adj.setdefault(v, set())
    return {u: sorted(nbrs) for u, nbrs in adj.items()}, duplicates


def _tarjan_scc(nodes, adj):
    """迭代版 Tarjan，返回 SCC 列表（每个 SCC 为节点 list）。"""
    index_of = {}
    low = {}
    on_stack = set()
    stack = []
    sccs = []
    for root in nodes:
        if root in index_of:
            continue
        work = [(root, 0)]
        while work:
            u, i = work[-1]
            if i == 0:
                index_of[u] = low[u] = len(index_of)
                stack.append(u)
                on_stack.add(u)
            nbrs = adj.get(u, ())
            descended = False
            while i < len(nbrs):
                v = nbrs[i]
                i += 1
                if v not in index_of:
                    work[-1] = (u, i)
                    work.append((v, 0))
                    descended = True
                    break
                if v in on_stack and index_of[v] < low[u]:
                    low[u] = index_of[v]
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                if low[u] < low[parent]:
                    low[parent] = low[u]
            if low[u] == index_of[u]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == u:
                        break
                sccs.append(scc)
    return sccs


def _enumerate_elementary_cycles(scc, adj, cap, step_budget=1_000_000):
    """在单个 SCC 内枚举基本环节点序列（每个环以最小节点开头并闭合）。

    返回 (cycles, truncated)。cycles 中每个元素形如 [n0, n1, ..., n0]。
    自环表示为 [s, s]。环数超过 cap 或搜索步数超过 step_budget 时停止
    枚举并置 truncated=True（环节点全集不受影响，由 SCC 保证完整）。
    """
    nodes = sorted(scc)
    cycles = []
    truncated = False
    steps = 0
    for idx, s in enumerate(nodes):
        allowed = set(nodes[idx:])  # 只走 >= s 的节点，保证每个基本环只枚举一次
        path = [s]
        on_path = {s}
        it_stack = [iter(adj.get(s, ()))]
        while it_stack:
            advanced = False
            for v in it_stack[-1]:
                steps += 1
                if steps > step_budget:
                    return cycles, True
                if v == s:
                    cycles.append(path + [s])
                    if len(cycles) >= cap:
                        return cycles, True
                elif v in allowed and v not in on_path:
                    path.append(v)
                    on_path.add(v)
                    it_stack.append(iter(adj.get(v, ())))
                    advanced = True
                    break
            if not advanced:
                it_stack.pop()
                if len(path) > 1:
                    on_path.discard(path.pop())
    return cycles, truncated


def find_cycles(graph, nodes=None, cycle_cap=100):
    """在 graph（或其子集 nodes）上检测环。

    返回环报告列表，按每个环区域的最小节点排序，元素为 dict：
      {
        "nodes": sorted list,            # 该环区域（SCC）中全部在环上的节点
        "cycles": [[n0, ..., n0], ...],  # 基本环节点序列
        "cycles_truncated": bool,        # 基本环枚举是否因 cycle_cap 被截断
      }
    无环时返回空列表。
    """
    if nodes is None:
        nodes = sorted(graph)
        adj = graph
    else:
        node_set = set(nodes)
        nodes = sorted(node_set)
        adj = {u: [v for v in graph.get(u, ()) if v in node_set] for u in nodes}
    reports = []
    for scc in _tarjan_scc(nodes, adj):
        if len(scc) == 1:
            s = scc[0]
            if s not in adj.get(s, ()):  # 无自环的单节点 SCC 不是环
                continue
        cycles, truncated = _enumerate_elementary_cycles(scc, adj, cycle_cap)
        reports.append({
            "nodes": sorted(scc),
            "cycles": cycles,
            "cycles_truncated": truncated,
        })
    reports.sort(key=lambda r: r["nodes"][0])
    return reports


def query(graph, start, max_depth, cycle_cap=100):
    """从 start 做深度受限遍历，同时进行环检测。

    返回 dict：
      status:    ok | depth_limited | cycle_detected |
                 cycle_detected_and_depth_limited
      visited:   {node: 最短深度}
      order:     访问顺序
      truncated: 因深度限制被截断（未继续展开）的节点列表
      cycles:    环报告列表（见 find_cycles），作用于深度限制内可达子图
    """
    visited = {start: 0}
    order = [start]
    truncated = []
    dq = deque([start])
    while dq:
        u = dq.popleft()
        depth = visited[u]
        if depth >= max_depth:
            if graph.get(u):
                truncated.append(u)
            continue
        for v in graph.get(u, ()):
            if v not in visited:
                visited[v] = depth + 1
                order.append(v)
                dq.append(v)
    cycles = find_cycles(graph, nodes=visited.keys(), cycle_cap=cycle_cap)
    if cycles and truncated:
        status = STATUS_BOTH
    elif cycles:
        status = STATUS_CYCLE
    elif truncated:
        status = STATUS_DEPTH_LIMITED
    else:
        status = STATUS_OK
    return {
        "status": status,
        "visited": visited,
        "order": order,
        "truncated": truncated,
        "cycles": cycles,
    }
