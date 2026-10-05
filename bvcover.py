"""二部图最小点覆盖求解库（仅标准库）。

方法：Hopcroft-Karp 求最大匹配，再用 König 定理的标准构造
（从左侧未匹配点出发沿交错路径可达集 Z，取 K = (L\\Z) ∪ (R∩Z)）
得到最小点覆盖。由 König 定理，二部图中 |最小点覆盖| = |最大匹配|，
因此覆盖大小与匹配规模相等即是最优性的直接证明。

输入约定：顶点为 0..n-1 的全局编号，side[v] 为 0（左）或 1（右），
边为无序对 (a, b)。同侧边（含 a == b 的自环）与多重边会被识别、
计数并报告；同侧边不属于二部图模型，从求解中剔除但绝不静默丢弃。
"""

from collections import deque
from dataclasses import dataclass

LEFT = 0
RIGHT = 1


@dataclass
class CoverResult:
    cover: list                 # 最小点覆盖中的全局顶点编号（升序）
    cover_size: int             # |覆盖|
    matching_size: int          # 最大匹配大小（= 最小点覆盖下界）
    n_vertices: int             # 顶点总数
    n_left: int
    n_right: int
    n_edges_input: int          # 输入边数（含重复、含非法边）
    n_edges_valid: int          # 去重且剔除同侧边后的有效边数
    n_duplicate_extra: int      # 多重边冗余条数
    duplicate_examples: list    # 多重边样例 [(a, b), ...]（全局编号，左端在前）
    n_same_side: int            # 同侧边 / 自环条数（非法，已剔除）
    same_side_examples: list    # 同侧边样例 [(a, b), ...]
    edges_checked: int          # 核验：逐一检查的有效边数
    edges_covered: int          # 核验：至少一个端点在覆盖中的边数

    @property
    def verified(self):
        """覆盖核验：每条有效边都至少有一个端点被覆盖。"""
        return self.edges_covered == self.edges_checked

    @property
    def optimal(self):
        """最小性核验：覆盖大小等于匹配规模下界。"""
        return self.cover_size == self.matching_size


def _hopcroft_karp(adj, n_left, n_right):
    """Hopcroft-Karp 最大匹配，O(E * sqrt(V))。DFS 用显式栈避免递归深度问题。"""
    pair_u = [-1] * n_left    # pair_u[u] = 与左点 u 匹配的右点，-1 表示未匹配
    pair_v = [-1] * n_right
    dist = [0] * n_left

    def bfs():
        q = deque()
        for u in range(n_left):
            if pair_u[u] == -1:
                dist[u] = 0
                q.append(u)
            else:
                dist[u] = -1
        level = -1  # 能到达空闲右点的最浅左点层数
        while q:
            u = q.popleft()
            if level != -1 and dist[u] >= level:
                continue
            for v in adj[u]:
                w = pair_v[v]
                if w == -1:
                    level = dist[u]
                elif dist[w] == -1:
                    dist[w] = dist[u] + 1
                    q.append(w)
        return level != -1

    def dfs(u0):
        """从空闲左点 u0 找一条增广路，找到则翻转并返回 True。"""
        us = [u0]                 # 路径上的左点栈
        vs = []                   # vs[i] 是从 us[i] 走向 us[i+1] 经过的右点
        its = [iter(adj[u0])]
        while us:
            u = us[-1]
            advanced = False
            for v in its[-1]:
                w = pair_v[v]
                if w == -1:
                    # 到达空闲右点，沿栈翻转整条增广路
                    v_cur = v
                    while us:
                        uu = us.pop()
                        its.pop()
                        pair_u[uu] = v_cur
                        pair_v[v_cur] = uu
                        if not vs:
                            break
                        v_cur = vs.pop()
                    return True
                if dist[w] == dist[u] + 1:
                    us.append(w)
                    vs.append(v)
                    its.append(iter(adj[w]))
                    advanced = True
                    break
            if not advanced:
                dist[u] = -1        # 本阶段标记为死路
                us.pop()
                its.pop()
                if len(vs) > len(us) - 1 and vs:
                    vs.pop()
        return False

    matching = 0
    while bfs():
        for u in range(n_left):
            if pair_u[u] == -1 and dfs(u):
                matching += 1
    return pair_u, pair_v, matching


def min_vertex_cover(n, side, edges, examples=10):
    """求二部图最小点覆盖。

    参数：
        n:       顶点数，顶点编号 0..n-1
        side:    长度为 n 的序列，side[v] ∈ {0(左), 1(右)}
        edges:   可迭代的 (a, b) 顶点对
        examples: 报告中最多保留的异常边样例条数

    返回：CoverResult。覆盖集保证覆盖所有有效边，且大小等于最大匹配。
    同侧边（含自环）与多重边在结果中如实报告。
    """
    if len(side) != n:
        raise ValueError("side 长度必须等于 n")
    for s in side:
        if s not in (LEFT, RIGHT):
            raise ValueError("side 只能取 0（左）或 1（右）")

    left_id = [-1] * n
    right_id = [-1] * n
    left_global = []
    right_global = []
    for v in range(n):
        if side[v] == LEFT:
            left_id[v] = len(left_global)
            left_global.append(v)
        else:
            right_id[v] = len(right_global)
            right_global.append(v)
    n_left = len(left_global)
    n_right = len(right_global)

    adj = [[] for _ in range(n_left)]
    seen = set()
    n_input = 0
    n_dup_extra = 0
    dup_examples = []
    n_same_side = 0
    same_side_examples = []

    for a, b in edges:
        n_input += 1
        if not (0 <= a < n and 0 <= b < n):
            raise ValueError("边端点越界: (%r, %r)" % (a, b))
        if side[a] == side[b]:
            # 同侧边（含 a == b 的自环）：不属于二部图模型，报告并剔除
            n_same_side += 1
            if len(same_side_examples) < examples:
                same_side_examples.append((a, b))
            continue
        u, v = (a, b) if side[a] == LEFT else (b, a)
        key = u * n + v
        if key in seen:
            n_dup_extra += 1
            if len(dup_examples) < examples:
                dup_examples.append((u, v))
            continue
        seen.add(key)
        adj[left_id[u]].append(right_id[v])

    pair_u, pair_v, matching = _hopcroft_karp(adj, n_left, n_right)

    # König 构造：从左侧未匹配点出发，沿 非匹配边(L->R) / 匹配边(R->L) 交错可达
    vis_l = bytearray(n_left)
    vis_r = bytearray(n_right)
    q = deque()
    for u in range(n_left):
        if pair_u[u] == -1:
            vis_l[u] = 1
            q.append((LEFT, u))
    while q:
        s, x = q.popleft()
        if s == LEFT:
            pu = pair_u[x]
            for v in adj[x]:
                if v != pu and not vis_r[v]:
                    vis_r[v] = 1
                    q.append((RIGHT, v))
        else:
            w = pair_v[x]
            if w != -1 and not vis_l[w]:
                vis_l[w] = 1
                q.append((LEFT, w))

    # 覆盖 K = (L \ Z) ∪ (R ∩ Z)
    cover = [left_global[u] for u in range(n_left) if not vis_l[u]]
    cover += [right_global[v] for v in range(n_right) if vis_r[v]]
    cover.sort()

    # 逐边核验：每条有效边至少一个端点在覆盖中
    checked = 0
    covered = 0
    for u in range(n_left):
        in_cover_u = not vis_l[u]
        for v in adj[u]:
            checked += 1
            if in_cover_u or vis_r[v]:
                covered += 1

    return CoverResult(
        cover=cover,
        cover_size=len(cover),
        matching_size=matching,
        n_vertices=n,
        n_left=n_left,
        n_right=n_right,
        n_edges_input=n_input,
        n_edges_valid=checked,
        n_duplicate_extra=n_dup_extra,
        duplicate_examples=dup_examples,
        n_same_side=n_same_side,
        same_side_examples=same_side_examples,
        edges_checked=checked,
        edges_covered=covered,
    )


def format_report(res):
    """把 CoverResult 格式化为人类可读的核验报告。"""
    lines = [
        "顶点: %d（左 %d / 右 %d）" % (res.n_vertices, res.n_left, res.n_right),
        "边: 输入 %d，有效（去重+剔除非法）%d" % (res.n_edges_input, res.n_edges_valid),
        "多重边: 冗余 %d 条" % res.n_duplicate_extra,
        "同侧边/自环: %d 条（非法，已剔除并报告）" % res.n_same_side,
        "最大匹配: %d" % res.matching_size,
        "最小点覆盖: %d" % res.cover_size,
        "覆盖核验: %d/%d 条有效边被覆盖 -> %s"
        % (res.edges_covered, res.edges_checked, "通过" if res.verified else "失败"),
        "最小性核验: |覆盖|=%d, 匹配下界=%d -> %s"
        % (res.cover_size, res.matching_size, "一致（最优）" if res.optimal else "不一致"),
    ]
    if res.duplicate_examples:
        lines.append("多重边样例: %s" % (res.duplicate_examples,))
    if res.same_side_examples:
        lines.append("同侧边样例: %s" % (res.same_side_examples,))
    return "\n".join(lines)
