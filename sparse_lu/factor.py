"""稀疏分块 LU（无选主元，右看 blocked right-looking）。

面板大小 block_size=B。第 p 个面板覆盖列/行 [s, e)，e=min(s+B,n)：

  1) 面板分解：依次以 k=s..e-1 为主元做消去，但更新只施加到面板内列 (k, e)，
     尾部列 (>=e) 的更新被延迟（分块的关键：批量更新、面板完成后可整体释放/溢写）。
  2) 面板行收尾：按行号递增，对面板内的行 i 施加本面板的延迟更新，然后取出 U[i]。
  3) 尾部更新：对所有受影响的行 i>=e，批量施加 -L[i,s:e]*U[s:e,e:]。
  4) 存储/溢写：面板 (L 列块、U 行块) 定版，可写入 spill_dir 释放内存。
  5) 每个检查点调用 MemoryGuard，超预算抛 MemoryBudgetExceeded（拒绝），进程存活。

存储模型：rows[i] 为行字典；colrows[j]={i>j : j in rows[i]} 记录列方向模式。
要求矩阵严格对角占优（测试矩阵均如此）；零/小主元抛 ZeroPivotError，不静默处理。
"""

import os
import pickle

from .memory import MemoryGuard


class ZeroPivotError(Exception):
    def __init__(self, k, value):
        super().__init__(
            f"第 {k} 个主元为零或过小 (|pivot|={value:.3e})。"
            "本分解不做部分选主元，请使用对角占优矩阵，或先做重排/数值稳定化。"
        )
        self.k = k
        self.value = value


class LUFactors:
    """L 单位下三角（按面板存行 {i: [(k, L_ik)]}），U 按面板存行字典。"""

    def __init__(self, n, panels, block_size, stats):
        self.n = n
        self.panels = panels
        self.block_size = block_size
        self.stats = stats

    def _load(self, panel):
        if "L" in panel:
            return panel
        with open(panel["path"], "rb") as fh:
            return pickle.load(fh)

    def nnz_l(self):
        return self.stats["l_nnz"]

    def nnz_u(self):
        return self.stats["u_nnz"]

    def solve(self, b):
        """前向（按面板、列方向）+ 回代（按面板逆序、行方向）求解 Ax=b。"""
        y = [float(v) for v in b]
        # L y = b，L 对角为 1
        for panel in self.panels:
            data = self._load(panel)
            for i in sorted(data["L"]):
                acc = y[i]
                for k, lik in data["L"][i]:
                    acc -= lik * y[k]
                y[i] = acc
        # U x = y
        x = y
        for panel in reversed(self.panels):
            data = self._load(panel)
            up = data["U"]
            for i in range(panel["e"] - 1, panel["s"] - 1, -1):
                ui = up[i]
                acc = x[i]
                diag = 1.0
                for j, v in ui.items():
                    if j == i:
                        diag = v
                    else:
                        acc -= v * x[j]
                x[i] = acc / diag
        return x


def _apply_update(ri, i, k, lik, uk, min_j, rows_live_nnz, colrows):
    """ri[j] -= lik * uk[j]，仅 j >= min_j；维护行数与列模式。返回新的 live 计数。"""
    for j, ukj in uk.items():
        if j < min_j:
            continue
        old = ri.get(j)
        if old is None:
            ri[j] = -lik * ukj
            rows_live_nnz += 1
            if i > j:
                colrows[j].add(i)
        else:
            nv = old - lik * ukj
            if nv == 0.0:
                del ri[j]
                rows_live_nnz -= 1
                if i > j:
                    colrows[j].discard(i)
            else:
                ri[j] = nv
    return rows_live_nnz


def factor(matrix, block_size=64, memory_limit_bytes=None, spill_dir=None,
           pivot_tol=1e-12, check_interval=256):
    n = matrix.n
    if block_size < 1:
        raise ValueError("block_size 必须 >= 1")
    rows = [dict(r) for r in matrix.rows]
    colrows = [set() for _ in range(n)]
    for i in range(n):
        for j in rows[i]:
            if i > j:
                colrows[j].add(i)

    guard = MemoryGuard(memory_limit_bytes) if memory_limit_bytes else None
    if spill_dir:
        os.makedirs(spill_dir, exist_ok=True)

    panels = []
    rows_live_nnz = sum(len(r) for r in rows)
    stored_nnz = 0  # 尚未溢写、仍驻留内存的 L+U 条目
    l_nnz = 0
    u_nnz = 0
    steps = 0

    s = 0
    while s < n:
        e = min(s + block_size, n)
        panel_l = {}  # i -> [(k, lik)...]，k 属于 [s,e)

        # ---- 1) 面板分解：尾部列更新延迟
        for k in range(s, e):
            rowk = rows[k]
            piv = rowk.get(k)
            if piv is None or abs(piv) <= pivot_tol:
                val = 0.0 if piv is None else piv
                raise ZeroPivotError(k, val)
            below = sorted(colrows[k])
            colrows[k] = set()
            rows_live_nnz -= len(below)  # 这些行随后 pop 掉列 k
            for i in below:
                ri = rows[i]
                aik = ri.pop(k)
                lik = aik / piv
                panel_l.setdefault(i, []).append((k, lik))
                l_nnz += 1
                for j, ukj in rowk.items():
                    if k < j < e:
                        old = ri.get(j)
                        if old is None:
                            ri[j] = -lik * ukj
                            rows_live_nnz += 1
                            if i > j:
                                colrows[j].add(i)
                        else:
                            nv = old - lik * ukj
                            if nv == 0.0:
                                del ri[j]
                                rows_live_nnz -= 1
                                if i > j:
                                    colrows[j].discard(i)
                            else:
                                ri[j] = nv
            steps += 1
            if guard is not None and steps % check_interval == 0:
                guard.check(rows_live_nnz + stored_nnz, context=f"面板内 pivot {k}")

        # ---- 2) 面板行：施加延迟更新并定版 U
        u_panel = {}
        for i in range(s, e):
            ri = rows[i]
            pl = panel_l.get(i)
            if pl:
                for k, lik in pl:
                    rows_live_nnz = _apply_update(
                        ri, i, k, lik, u_panel[k], e,
                        rows_live_nnz, colrows)
            ui = {j: v for j, v in ri.items() if j >= i}
            u_panel[i] = ui
            u_nnz += len(ui)
            rows_live_nnz -= len(ri)
            rows[i] = {}

        # ---- 3) 尾部行：批量施加 L*U 更新
        for i in sorted(panel_l):
            if i < e:
                continue
            ri = rows[i]
            for k, lik in panel_l[i]:
                rows_live_nnz = _apply_update(
                    ri, i, k, lik, u_panel[k], e,
                    rows_live_nnz, colrows)

        # ---- 4) 定版面板块，内存驻留或溢写磁盘
        panel_l_nnz = sum(len(pl) for pl in panel_l.values())
        panel_u_nnz = sum(len(ui) for ui in u_panel.values())
        stored_nnz += panel_l_nnz + panel_u_nnz
        if spill_dir is not None:
            path = os.path.join(spill_dir, f"panel_{len(panels):06d}.pkl")
            with open(path, "wb") as fh:
                pickle.dump({"s": s, "e": e, "L": panel_l, "U": u_panel},
                            fh, protocol=pickle.HIGHEST_PROTOCOL)
            stored_nnz -= panel_l_nnz + panel_u_nnz
            panels.append({"s": s, "e": e, "path": path})
        else:
            panels.append({"s": s, "e": e, "L": panel_l, "U": u_panel})

        # ---- 5) 内存守卫
        if guard is not None:
            guard.check(rows_live_nnz + stored_nnz, context=f"面板 [0-based {s}:{e}]")
        s = e

    stats = {
        "l_nnz": l_nnz,
        "u_nnz": u_nnz,
        "lu_nnz": l_nnz + u_nnz,
        "fill": l_nnz + u_nnz - matrix.nnz,
        "a_nnz": matrix.nnz,
        "block_size": block_size,
        "panels": len(panels),
        "spilled": spill_dir is not None,
        "peak_live_entries": guard.peak_entries if guard else (rows_live_nnz + stored_nnz),
    }
    return LUFactors(n, panels, block_size, stats)


def factor_residual(lu, matrix):
    """||P A - L U||_F / ||A||_F（这里 P=I：在已重排矩阵上分解时 A 指重排后的矩阵）。"""
    d = [dict(r) for r in matrix.rows]
    for panel in lu.panels:
        data = lu._load(panel)
        up = data["U"]
        for i, pl in data["L"].items():
            di = d[i]
            for k, lik in pl:
                uk = up[k]
                for j, ukj in uk.items():
                    di[j] = di.get(j, 0.0) - lik * ukj
        for i, ui in up.items():
            di = d[i]
            for j, v in ui.items():
                di[j] = di.get(j, 0.0) - v
    num = 0.0
    for di in d:
        for v in di.values():
            num += v * v
    import math
    return math.sqrt(num) / matrix.frobenius_norm()
