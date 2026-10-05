"""community.py -- 确定性 Louvain 社区发现（仅 Python 3 标准库）。

设计要点
--------
* 图以 CSR（压缩稀疏行）存储：offsets / neighbors / weights 三个扁平数组。
* 模块度增益用整数比较（把 2m 乘到两边），不存在浮点平局问题；
  平局时取社区编号最小者，节点遍历顺序由固定种子的 random.Random 洗牌，
  因此同一种子两次运行结果逐位一致。
* 孤立点（加权度为 0）永远自成单点社区；只带自环的点在增益上无利可图，
  也保持单点社区 —— 两者归属完全确定，不做随机分配。
* 终止条件：单层内连续一整轮没有任何节点移动即收敛；层间当社区数不再
  减少时停止。迭代上界：每层最多 MAX_PASSES 轮，最多 MAX_LEVELS 层
  （每收敛一层社区数严格下降，n 个点最多 n-1 层，双重上界）。
"""

from array import array
import random

MAX_PASSES = 100   # 单层局部移动的最大轮数（实际 3~10 轮即收敛）
MAX_LEVELS = 64    # 最大聚合层数（理论自然上界为 n-1）
EPS_GAIN = 1e-10   # 最小增益阈值（增益为整数比较，该阈值只防 0 增益抖动）


class Graph:
    """无向加权图的 CSR 表示。自环只存一条，普通边存双向两条。"""

    __slots__ = ("n", "offsets", "neighbors", "weights", "m2")

    def __init__(self, n, offsets, neighbors, weights, m2):
        self.n = n
        self.offsets = offsets      # 长度 n+1 的前缀和
        self.neighbors = neighbors  # 长度 m2
        self.weights = weights      # 长度 m2，与 neighbors 对齐
        self.m2 = m2                # 加权度总和 = 2 * 总边权


def build_csr(n, edges):
    """由边列表构建 CSR。edges 为 (u, v) 或 (u, v, w)，w 默认 1。

    平行边自动合并求和；自环保留（只存一条）；输入顺序不影响结果
    （每个点的邻接按邻居编号升序），保证可复现。
    """
    pairs = {}
    for e in edges:
        if len(e) == 2:
            u, v, w = e[0], e[1], 1
        else:
            u, v, w = e[0], e[1], e[2]
        if u == v:
            pairs[(u, u)] = pairs.get((u, u), 0) + w
        else:
            if u > v:
                u, v = v, u
            pairs[(u, v)] = pairs.get((u, v), 0) + w

    deg = [0] * n
    for (u, v) in pairs:
        if u == v:
            deg[u] += 1
        else:
            deg[u] += 1
            deg[v] += 1

    offsets = array("q", [0])  # 前缀和
    acc = 0
    for d in deg:
        acc += d
        offsets.append(acc)

    neighbors = array("i", bytes(4 * acc))
    weights = array("q", bytes(8 * acc))
    cursor = list(offsets[:-1])
    for (u, v), w in sorted(pairs.items()):
        if u == v:
            i = cursor[u]
            neighbors[i] = v
            weights[i] = w
            cursor[u] += 1
        else:
            i = cursor[u]
            neighbors[i] = v
            weights[i] = w
            cursor[u] += 1
            j = cursor[v]
            neighbors[j] = u
            weights[j] = w
            cursor[v] += 1

    m2 = 0
    for (u, v), w in pairs.items():
        m2 += w if u == v else 2 * w
    return Graph(n, offsets, neighbors, weights, m2)


def build_csr_arrays(n, edges_flat):
    """基准专用：edges_flat 为扁平 array('i')，[u0, v0, u1, v1, ...]，权重全 1。

    不做去重（基准生成器保证无重复边），省去 dict 合并以压低内存峰值。
    """
    deg = [0] * n
    m = len(edges_flat) // 2
    for k in range(m):
        u = edges_flat[2 * k]
        v = edges_flat[2 * k + 1]
        if u == v:
            deg[u] += 1
        else:
            deg[u] += 1
            deg[v] += 1

    offsets = array("q", [0])
    acc = 0
    for d in deg:
        acc += d
        offsets.append(acc)

    neighbors = array("i", bytes(4 * acc))
    weights = array("q", bytes(8 * acc))
    cursor = list(offsets[:-1])
    for k in range(m):
        u = edges_flat[2 * k]
        v = edges_flat[2 * k + 1]
        if u == v:
            i = cursor[u]
            neighbors[i] = v
            weights[i] = 1
            cursor[u] += 1
        else:
            i = cursor[u]
            neighbors[i] = v
            weights[i] = 1
            cursor[u] += 1
            j = cursor[v]
            neighbors[j] = u
            weights[j] = 1
            cursor[v] += 1
    return Graph(n, offsets, neighbors, weights, acc)


def _weighted_degrees(g):
    deg = [0] * g.n
    for u in range(g.n):
        s = 0
        for i in range(g.offsets[u], g.offsets[u + 1]):
            s += g.weights[i]
        deg[u] = s
    return deg


def _local_move(g, rng):
    """单层 Louvain 局部移动。返回 (node_comm, nb_passes)。

    增益（把节点 u 从当前社区移入社区 C）的精确形式：
        dQ = [k_i_in - k_i * tot_C / m] / m        （m = 总边权 = m2/2）
    为避免浮点，比较整数 delta = k_i_in * m - k_i * tot_C，dQ > 0 <=> delta > 0。
    自环不进入 k_i_in（遍历邻居时跳过 u==u），因此对增益无影响。
    """
    n = g.n
    offsets = g.offsets
    neighbors = g.neighbors
    weights = g.weights
    m2 = g.m2  # 2 * 总边权；增益比较统一乘 m2，无需除法，整数精确

    deg = _weighted_degrees(g)
    node_comm = list(range(n))
    comm_tot = deg[:]  # 社区内所有点的加权度之和

    order = list(range(n))
    nb_passes = 0
    for _ in range(MAX_PASSES):
        nb_passes += 1
        rng.shuffle(order)
        moved = 0
        for u in order:
            du = deg[u]
            if du == 0:
                continue  # 孤立点：永远留在自己的单点社区
            cur = node_comm[u]

            # 统计 u 到各邻居社区的连接权（自环不计）
            nbr = {}
            for i in range(offsets[u], offsets[u + 1]):
                v = neighbors[i]
                if v == u:
                    continue
                c = node_comm[v]
                nbr[c] = nbr.get(c, 0) + weights[i]

            k_i_in_cur = nbr.get(cur, 0)
            comm_tot[cur] -= du
            node_comm[u] = -1

            best_c = cur
            best_delta = k_i_in_cur * m2 - du * comm_tot[cur]
            for c, w in nbr.items():
                if c == cur:
                    continue
                delta = w * m2 - du * comm_tot[c]
                if delta > best_delta or (delta == best_delta and c < best_c):
                    best_delta = delta
                    best_c = c

            if best_c != cur and best_delta > 0:
                node_comm[u] = best_c
                comm_tot[best_c] += du
                moved += 1
            else:
                node_comm[u] = cur
                comm_tot[cur] += du
        if moved == 0:
            break
    return node_comm, nb_passes


def _renumber(node_comm):
    """把社区标签压缩为 0..k-1（按首次出现顺序），返回 (labels, k)。"""
    remap = {}
    labels = [0] * len(node_comm)
    nxt = 0
    for u, c in enumerate(node_comm):
        r = remap.get(c)
        if r is None:
            r = nxt
            remap[c] = r
            nxt += 1
        labels[u] = r
    return labels, nxt


def _coarsen(g, node_comm, k):
    """按社区把图聚合成超点图。超点 c 的加权度 = 原社区 tot_c，守恒。"""
    agg = {}
    for u in range(g.n):
        cu = node_comm[u]
        for i in range(g.offsets[u], g.offsets[u + 1]):
            v = g.neighbors[i]
            w = g.weights[i]
            cv = node_comm[v]
            if cu == cv:
                key = (cu, cu)
                agg[key] = agg.get(key, 0) + w
            elif cu < cv:
                key = (cu, cv)
                agg[key] = agg.get(key, 0) + w
            # cu > cv 的槽位是同一条无向边的反向，跳过避免重复

    deg = [0] * k
    for (cu, cv) in agg:
        if cu == cv:
            deg[cu] += 1
        else:
            deg[cu] += 1
            deg[cv] += 1

    offsets = [0] * (k + 1)
    for c in range(k):
        offsets[c + 1] = offsets[c] + deg[c]
    total = offsets[k]
    neighbors = [0] * total
    weights = [0] * total
    cursor = offsets[:-1]
    for (cu, cv), w in sorted(agg.items()):
        if cu == cv:
            i = cursor[cu]
            neighbors[i] = cv
            weights[i] = w
            cursor[cu] += 1
        else:
            i = cursor[cu]
            neighbors[i] = cv
            weights[i] = w
            cursor[cu] += 1
            j = cursor[cv]
            neighbors[j] = cu
            weights[j] = w
            cursor[cv] += 1
    m2 = 0
    for (cu, cv), w in agg.items():
        m2 += w if cu == cv else 2 * w
    return Graph(k, offsets, neighbors, weights, m2)


def canonical_labels(labels):
    """社区标签规范化：按社区内最小点编号排序，重映射为 0..k-1。

    规范化后，两次运行只要划分相同，标签序列逐位一致，可直接对拍。
    """
    mins = {}
    for u, c in enumerate(labels):
        if c not in mins or u < mins[c]:
            mins[c] = u
    order = sorted(mins, key=lambda c: mins[c])
    remap = {c: i for i, c in enumerate(order)}
    return [remap[c] for c in labels]


def louvain(graph, seed=42):
    """对 CSR 图运行 Louvain，返回 (labels, info)。

    labels 为规范化后的社区标签（0..k-1）；info 含层数、各层轮数、
    社区数、模块度等诊断信息。同一 (graph, seed) 结果完全一致。
    """
    rng = random.Random(seed)
    hierarchy = []  # 每层：原图节点 -> 该层超点 的映射
    passes = []
    g = graph
    level = 0
    while True:
        node_comm, nb = _local_move(g, rng)
        passes.append(nb)
        mapping, k = _renumber(node_comm)
        hierarchy.append(mapping)
        if k == g.n or level + 1 >= MAX_LEVELS:
            break
        g = _coarsen(g, mapping, k)
        level += 1

    # 把各层映射复合回原始节点：u -> h0[u] -> h1[h0[u]] -> ...
    labels = list(range(graph.n))
    for mapping in hierarchy:
        labels = [mapping[c] for c in labels]
    labels = canonical_labels(labels)

    info = {
        "levels": len(hierarchy),
        "passes_per_level": passes,
        "num_communities": len(set(labels)),
        "seed": seed,
    }
    return labels, info


def modularity(graph, labels):
    """标准模块度 Q = sum_c [ in_c/(2m) - (tot_c/(2m))^2 ]，自环计入 in_c。"""
    m2 = graph.m2
    if m2 == 0:
        return 0.0
    in_w = {}
    tot = {}
    for u in range(graph.n):
        c = labels[u]
        tot[c] = tot.get(c, 0)  # 确保键存在
        for i in range(graph.offsets[u], graph.offsets[u + 1]):
            v = graph.neighbors[i]
            w = graph.weights[i]
            tot[c] += w
            if labels[v] == c:
                in_w[c] = in_w.get(c, 0) + w
    q = 0.0
    for c, t in tot.items():
        q += in_w.get(c, 0) / m2 - (t / m2) ** 2
    return q


def detect(n, edges, seed=42):
    """便捷接口：detect(n, [(u, v), ...]) -> (labels, info)。"""
    g = build_csr(n, edges)
    labels, info = louvain(g, seed=seed)
    info["modularity"] = modularity(g, labels)
    return labels, info
