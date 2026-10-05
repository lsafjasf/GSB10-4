"""二部图最小点覆盖（König 定理），仅依赖标准库。

方法：
  1. 用 Hopcroft-Karp 求最大匹配 M，时间 O(E * sqrt(V))。
  2. König 构造：从所有"未匹配的左部点"出发，沿交错路
     （左->右 走非匹配边，右->左 走匹配边）做 BFS，记可达点集为 Z。
  3. 最小点覆盖 C = (L \\ Z) ∪ (R ∩ Z)。

为什么是最小覆盖（正确性要点）：
  * C 是覆盖：若存在边 (u,v)（u∈L, v∈R）两端都不在 C 中，
    则 u∈Z 且 v∉Z。u∈Z 说明 u 从某个未匹配左点交错可达；
    若 (u,v) 是非匹配边，则 v 也应可达（v∈Z），矛盾；
    若 (u,v) 是匹配边，则 u 只能经由 v 的匹配边被到达，
    即到达 u 的交错路必经 v，故 v∈Z，同样矛盾。
  * |C| = |M|：匹配边两端要么都在 Z 外/内的对应侧，构造保证
    每条匹配边恰好贡献 C 中的一个端点，非匹配边不额外贡献，
    因此 |C| = |M|。
  * 任意点覆盖至少要覆盖 |M| 条两两不相交的匹配边，故 |C| >= |M|
    是普遍下界。构造出的 C 达到该下界，所以是最小点覆盖。

多重边与自环处理：
  * 多重边（同一对端点重复出现）：记录到 result.multi_edges 并去重，
    去重不影响匹配与覆盖的正确性（覆盖一条即覆盖全部平行边）。
  * 自环（u == v）与同侧边（两端点在同一侧）：图不再是二部图，
    记录到 result.anomalies 并从二部求解中剔除，同时在报告中显式
    列出，绝不静默忽略。注意：若把自环视为必须覆盖的边，则其端点
    必须额外加入覆盖，本库将其如实报告，由调用方决定。
"""

from collections import deque

__all__ = ["BipartiteGraph", "CoverResult"]


class CoverResult:
    """一次最小点覆盖求解的结果。"""

    def __init__(self, graph, cover_left, cover_right, matching_size):
        self.graph = graph
        self.cover_left = sorted(cover_left)
        self.cover_right = sorted(cover_right)
        self.matching_size = matching_size

    @property
    def cover(self):
        """覆盖点集（原始标签）。"""
        return self.cover_left + self.cover_right

    @property
    def cover_size(self):
        return len(self.cover_left) + len(self.cover_right)

    @property
    def anomalies(self):
        return self.graph.anomalies

    @property
    def multi_edges(self):
        return self.graph.multi_edges

    def uncovered_edges(self):
        """逐边核验：返回未被覆盖的边列表（空列表 = 全部覆盖）。"""
        cl = set(self.cover_left)
        cr = set(self.cover_right)
        return [(u, v) for (u, v) in self.graph.edges
                if u not in cl and v not in cr]

    def is_valid_cover(self):
        return not self.uncovered_edges()

    def is_minimum(self):
        """最小性核验：覆盖大小 == 最大匹配规模（König 下界取等）。"""
        return self.is_valid_cover() and self.cover_size == self.matching_size

    def report(self):
        lines = []
        lines.append("=== 最小点覆盖报告 ===")
        lines.append(f"左部点数: {len(self.graph.left)}, 右部点数: {len(self.graph.right)}")
        lines.append(f"唯一边数: {len(self.graph.edges)}")
        lines.append(f"最大匹配规模 |M|: {self.matching_size}")
        lines.append(f"最小点覆盖规模 |C|: {self.cover_size}")
        lines.append(f"下界对照: |C| == |M| ? {self.cover_size == self.matching_size}")
        uncov = self.uncovered_edges()
        lines.append(f"逐边核验: 未覆盖边数 = {len(uncov)}"
                     + ("" if not uncov else f" -> {uncov[:10]}"))
        if self.multi_edges:
            lines.append(f"多重边（已去重，共 {len(self.multi_edges)} 条重复）: "
                         f"{self.multi_edges[:10]}"
                         + (" ..." if len(self.multi_edges) > 10 else ""))
        else:
            lines.append("多重边: 无")
        if self.anomalies:
            lines.append(f"异常边（自环/同侧边，已剔除出二部求解，共 {len(self.anomalies)} 条）:")
            for kind, u, v in self.anomalies[:10]:
                lines.append(f"  - [{kind}] {u!r} -- {v!r}")
            if len(self.anomalies) > 10:
                lines.append(f"  ... 其余 {len(self.anomalies) - 10} 条略")
        else:
            lines.append("异常边（自环/同侧边）: 无")
        return "\n".join(lines)


class BipartiteGraph:
    """二部图。顶点用可哈希标签表示，左右两侧标签集合必须不相交。"""

    def __init__(self, left_nodes=(), right_nodes=()):
        self.left = set(left_nodes)
        self.right = set(right_nodes)
        overlap = self.left & self.right
        if overlap:
            raise ValueError(f"左右两侧标签集合相交: {sorted(overlap)[:5]}")
        self.edges = []          # 去重后的唯一边 (u, v)，u∈L, v∈R
        self.multi_edges = []    # 重复出现的边（多重边）
        self.anomalies = []      # ('self_loop'|'same_side'|'unknown_vertex', u, v)
        self._edge_set = set()

    def add_edge(self, u, v):
        if u == v:
            self.anomalies.append(("self_loop", u, v))
            return
        u_l, u_r = u in self.left, u in self.right
        v_l, v_r = v in self.left, v in self.right
        if (u_l and v_l) or (u_r and v_r):
            self.anomalies.append(("same_side", u, v))
            return
        if u_r and v_l:
            u, v = v, u  # 统一成 (左, 右) 方向
        elif not (u_l and v_r):
            self.anomalies.append(("unknown_vertex", u, v))
            return
        if (u, v) in self._edge_set:
            self.multi_edges.append((u, v))
            return
        self._edge_set.add((u, v))
        self.edges.append((u, v))

    def add_edges(self, pairs):
        for u, v in pairs:
            self.add_edge(u, v)

    # ---- Hopcroft-Karp 最大匹配（迭代式 DFS，避免递归深度问题） ----
    def _max_matching(self, adj, n_left, n_right):
        pair_u = [-1] * n_left
        pair_v = [-1] * n_right
        dist = [0] * n_left

        def bfs():
            dq = deque()
            found = False
            for u in range(n_left):
                if pair_u[u] == -1:
                    dist[u] = 0
                    dq.append(u)
                else:
                    dist[u] = -1
            while dq:
                u = dq.popleft()
                du = dist[u] + 1
                for v in adj[u]:
                    w = pair_v[v]
                    if w == -1:
                        found = True
                    elif dist[w] == -1:
                        dist[w] = du
                        dq.append(w)
            return found

        def augment(u0):
            # 从自由左点 u0 出发的迭代 DFS，找到增广路则增广并返回 True
            left_stack = [u0]          # 路径上的左点
            right_stack = []           # right_stack[i]: left_stack[i] 与 left_stack[i+1] 之间的右点
            iters = [iter(adj[u0])]
            while left_stack:
                u = left_stack[-1]
                action = None
                for v in iters[-1]:
                    w = pair_v[v]
                    if w == -1:
                        action = ("free", v)
                        break
                    if dist[w] == dist[u] + 1:
                        action = ("go", v, w)
                        break
                if action is None:
                    dist[u] = -1
                    left_stack.pop()
                    iters.pop()
                    if right_stack:
                        right_stack.pop()
                elif action[0] == "free":
                    cur_v = action[1]
                    for i in range(len(left_stack) - 1, -1, -1):
                        uu = left_stack[i]
                        pair_u[uu] = cur_v
                        pair_v[cur_v] = uu
                        if i > 0:
                            cur_v = right_stack[i - 1]
                    return True
                else:
                    _, v, w = action
                    right_stack.append(v)
                    left_stack.append(w)
                    iters.append(iter(adj[w]))
            return False

        matching = 0
        while bfs():
            for u in range(n_left):
                if pair_u[u] == -1 and dist[u] != -1:
                    if augment(u):
                        matching += 1
        return matching, pair_u, pair_v

    # ---- König 构造最小点覆盖 ----
    def min_vertex_cover(self):
        left_labels = sorted(self.left)
        right_labels = sorted(self.right)
        li = {x: i for i, x in enumerate(left_labels)}
        ri = {x: i for i, x in enumerate(right_labels)}
        n_left, n_right = len(left_labels), len(right_labels)

        adj = [[] for _ in range(n_left)]
        for u, v in self.edges:
            adj[li[u]].append(ri[v])

        matching, pair_u, pair_v = self._max_matching(adj, n_left, n_right)

        # 从未匹配左点出发的交错 BFS：左->右 非匹配边，右->左 匹配边
        in_z_l = [False] * n_left
        in_z_r = [False] * n_right
        dq = deque()
        for u in range(n_left):
            if pair_u[u] == -1:
                in_z_l[u] = True
                dq.append(u)
        while dq:
            u = dq.popleft()
            mu = pair_u[u]
            for v in adj[u]:
                if v != mu and not in_z_r[v]:
                    in_z_r[v] = True
                    w = pair_v[v]
                    if w != -1 and not in_z_l[w]:
                        in_z_l[w] = True
                        dq.append(w)

        cover_left = [left_labels[u] for u in range(n_left) if not in_z_l[u]]
        cover_right = [right_labels[v] for v in range(n_right) if in_z_r[v]]
        return CoverResult(self, cover_left, cover_right, matching)
