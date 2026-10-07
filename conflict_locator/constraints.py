"""约束建模与可满足性判定（仅标准库）。

约束语言：差分约束（difference constraints）
    x_u - x_v <= c        （c 为整数）
    x_u - x_v <  c   <=>  x_u - x_v <= c - 1   （整数域下严格式归约）

建模：变量 x_1..x_n 对应图顶点 1..n，约束 x_u - x_v <= c 对应有向边 v -> u（权 c）。
定理：约束系统不可满足 <=> 对应图中存在总权为负的有向环（Bellman-Ford 标准结论）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

INF = float("inf")


@dataclass(frozen=True)
class Constraint:
    """一条差分约束 x_u - x_v <= c。"""

    u: int
    v: int
    c: int
    cid: str = ""

    def is_self_contradictory(self) -> bool:
        """单条即不可满足：x - x <= c 且 c < 0。"""
        return self.u == self.v and self.c < 0

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"[{self.cid}] x{self.u} - x{self.v} <= {self.c}"


def normalize_ids(constraints: Sequence[Constraint]) -> List[Constraint]:
    """为缺省 cid 的约束补发稳定编号 C1..Cm；已有 cid 保持不变。"""
    out: List[Constraint] = []
    for i, con in enumerate(constraints):
        cid = con.cid or f"C{i + 1}"
        out.append(Constraint(con.u, con.v, con.c, cid))
    return out


@dataclass
class CheckResult:
    feasible: bool
    assignment: Optional[Dict[int, int]] = None   # 可满足时的一组解（势函数）
    neg_cycle_cids: Optional[List[str]] = None    # 不可满足时一个负环上的约束编号
    neg_cycle_weight: Optional[int] = None
    stats: Optional[dict] = None


class DifferenceSystem:
    """差分约束系统：建图 + Bellman-Ford 判定。"""

    def __init__(self, n_vars: int, constraints: Sequence[Constraint]):
        self.n_vars = n_vars
        self.constraints: List[Constraint] = normalize_ids(constraints)
        bad = [c for c in self.constraints if c.u < 1 or c.v < 1]
        if bad:
            raise ValueError(f"变量编号必须从 1 开始，非法约束: {bad[0]}")
        if n_vars < max((c.u for c in self.constraints), default=0):
            raise ValueError("n_vars 小于约束中出现的变量编号")
        self.n = n_vars + 1  # 顶点 0 为超级源点
        # 边: (from, to, weight, 约束下标)；超级源边 cid 下标为 -1
        self.edges: List[Tuple[int, int, int, int]] = []
        for v in range(1, self.n):
            self.edges.append((0, v, 0, -1))
        for i, con in enumerate(self.constraints):
            self.edges.append((con.v, con.u, con.c, i))
        self.out_map: Dict[int, List[Tuple[int, int, int]]] = {}
        for (a, b, w, cid) in self.edges:
            self.out_map.setdefault(a, []).append((b, w, cid))

    def check(self, keep: Optional[Sequence[bool]] = None) -> CheckResult:
        """Bellman-Ford 判定。keep 指定启用的约束（None 表示全部启用）。

        终止条件：
          * 某一轮没有任何松弛成功 -> 收敛，系统可满足；
          * 第 n 轮仍有松弛成功 -> 存在负环，系统不可满足；
          * 提前检测：每轮结束后沿前驱链回走，若回到本轮被更新的顶点，
            则已能提取负环，提前终止（正确性：沿前驱链回走 n 步必含环，
            该环必为负环，见 _extract_predecessor_cycle）。
        """
        n = self.n
        dist = [INF] * n
        pred = [-1] * n
        dist[0] = 0
        edges = self.edges
        if keep is None:
            active = edges
        else:
            active = [e for e in edges if e[3] < 0 or keep[e[3]]]
        adj: Dict[int, List[Tuple[int, int, int]]] = {}
        for (a, b, w, cid) in active:
            adj.setdefault(a, []).append((b, w, cid))

        last = -1
        passes = 0
        for i in range(1, n + 1):
            passes = i
            last = -1
            for (a, b, w, _cid) in active:
                da = dist[a]
                if da != INF and da + w < dist[b]:
                    dist[b] = da + w
                    pred[b] = a
                    last = b
            if last == -1:
                break  # 收敛：可满足
            cyc = self._extract_predecessor_cycle(pred, last, adj)
            if cyc is not None:
                edge_ids, total = cyc
                return self._infeasible(edge_ids, total, passes, early=True)
            if i == n:
                x = last
                for _ in range(n):
                    x = pred[x]
                edge_ids, total = self._walk_cycle(pred, x, adj)
                return self._infeasible(edge_ids, total, passes, early=False)

        assignment = {v: int(dist[v]) for v in range(1, n)}
        return CheckResult(True, assignment=assignment,
                           stats={"passes": passes, "edges": len(active)})

    # ---- 内部 ----

    def _infeasible(self, edge_ids: List[int], total: int, passes: int,
                    early: bool) -> CheckResult:
        cids = [self.constraints[i].cid for i in edge_ids if i >= 0]
        return CheckResult(False, neg_cycle_cids=cids, neg_cycle_weight=total,
                           stats={"passes": passes, "early_cycle": early})

    def _walk_cycle(self, pred: List[int], start: int,
                    adj: Dict[int, List[Tuple[int, int, int]]]
                    ) -> Tuple[List[int], int]:
        """从 start 沿前驱链走回 start，收集环上的约束边下标与总权。"""
        edge_ids: List[int] = []
        total = 0
        x = start
        while True:
            p = pred[x]
            best = None
            for (b, w, cid) in adj.get(p, ()):
                if b == x and (best is None or w < best[0]):
                    best = (w, cid)
            edge_ids.append(best[1])
            total += best[0]
            x = p
            if x == start:
                break
        return edge_ids, total

    def _extract_predecessor_cycle(self, pred: List[int], start: int,
                                   adj: Dict[int, List[Tuple[int, int, int]]]
                                   ) -> Optional[Tuple[List[int], int]]:
        """沿前驱链从 start 回走，若再次遇到 start 则提取环并验证总权为负。

        返回 (约束边下标列表, 总权)；找不到环或总权非负返回 None。
        正确性说明：第 i 轮结束时 dist 对应不超过 i 条边的最短路径；若第 n 轮
        仍可松弛，则最短路含 >= n 条边，必含环且环总权为负。提前回走得到的环
        上的边均为本轮实际用来松弛的激活边（或其平行边），显式校验总权 < 0
        后才采信，故不会误报。
        """
        n = self.n
        seen_at: Dict[int, int] = {}
        path: List[int] = []
        x = start
        for _ in range(n + 1):
            if x in seen_at:
                seg = path[seen_at[x]:]  # pred 方向上的环段
                # 前驱关系 pred[seg[j]] = seg[j+1] 表示边 seg[j+1] -> seg[j]，
                # 故环上的有向边为 seg[k] -> seg[k-1]（下标取模）。
                edge_ids: List[int] = []
                total = 0
                for k in range(len(seg)):
                    a = seg[k]
                    b = seg[k - 1]
                    best = None
                    for (eb, w, cid) in adj.get(a, ()):
                        if eb == b and (best is None or w < best[0]):
                            best = (w, cid)
                    if best is None:
                        return None
                    edge_ids.append(best[1])
                    total += best[0]
                if total < 0:
                    return edge_ids, total
                return None
            seen_at[x] = len(path)
            path.append(x)
            p = pred[x]
            if p < 0:
                return None
            x = p
        return None
