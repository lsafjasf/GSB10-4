"""演示：特殊情形逐一求解并打印核验/对照数据。

运行: python3 demo.py
"""

from bipartite_cover import BipartiteGraph


def show(title, n_left, n_right, edges, extra=None):
    print(f"\n########## {title} ##########")
    g = BipartiteGraph([("L", i) for i in range(n_left)],
                       [("R", j) for j in range(n_right)])
    for u, v in edges:
        g.add_edge(("L", u) if isinstance(u, int) else u,
                   ("R", v) if isinstance(v, int) else v)
    if extra:
        extra(g)
    res = g.min_vertex_cover()
    print(res.report())
    cl = set(res.cover_left)
    cr = set(res.cover_right)
    print(f"覆盖点集 C = {sorted(res.cover)}")
    # 逐边核验数据：列出每条边被哪个端点覆盖
    print("逐边核验明细:")
    for (u, v) in g.edges:
        by = []
        if u in cl:
            by.append(u)
        if v in cr:
            by.append(v)
        print(f"  边 {u}--{v}: 覆盖端点 {by}")


show("1. 覆盖单边", 1, 1, [(0, 0)])
show("2. 不连通图（单边组件 + 星形组件）", 2, 3, [(0, 0), (1, 1), (1, 2)])
show("3. 一侧为空", 4, 0, [])
show("4. 含多重边", 2, 2, [(0, 0), (0, 0), (0, 0), (0, 1), (1, 1)])


def add_anomalies(g):
    g.add_edge(("L", 0), ("L", 1))   # 同侧边
    g.add_edge(("L", 1), ("L", 1))   # 自环


show("5. 含自环与同侧边（识别并报告）", 2, 2, [(0, 0), (1, 1)], extra=add_anomalies)
