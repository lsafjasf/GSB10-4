"""transform2d.py — 2D 仿射变换矩阵的合成与应用（纯标准库）。

乘序约定（见 README.md 的数学说明）
----------------------------------
* 点用列向量表示：p = [x, y, 1]^T，矩阵左乘：p' = M · p。
* 本库的 6 元组 (a, b, c, d, e, f) 表示齐次 3x3 矩阵::

        | a  c  e |
    M = | b  d  f |
        | 0  0  1 |

  即 x' = a*x + c*y + e,  y' = b*x + d*y + f。
* 复合乘法 :func:`mat_mul(m1, m2)` 返回 m1 @ m2，含义是“先作用 m2，再作用 m1”。
* 场景图中节点世界矩阵  W_node = M_root @ ... @ M_parent @ M_local，
  按 W_child = W_parent @ M_local 逐层累积。

奇异矩阵
--------
线性部分行列式 det = a*d - b*c 为 0 即不可逆（带尺度自适应容差）。
奇异节点不会被静默跳过：遍历时生成 :class:`SingularReport`，其几何仍然被变换
（坍缩为线段或点），包围盒由角点变换取极值得到，始终有限；任何需要逆矩阵的
操作显式抛出 :class:`SingularMatrixError`，绝不会产生 inf/nan。

遍历
----
合成与包围盒重算全部使用显式栈迭代，不递归，深链不会触发 RecursionError。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

__all__ = [
    "IDENTITY",
    "BBox",
    "SingularMatrixError",
    "SingularReport",
    "Node",
    "mat_mul",
    "mat_inv",
    "apply",
    "det",
    "is_singular",
    "rank2",
    "rotation",
    "scale",
    "translation",
    "transform_bbox",
    "bbox_union",
    "bbox_by_sampling",
    "compute_world_matrices",
    "compute_world_bboxes",
    "update_scene",
]

# (a, b, c, d, e, f)，见模块文档字符串中的矩阵布局
Mat = tuple[float, float, float, float, float, float]
BBox = tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y)

IDENTITY: Mat = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

_SINGULAR_RTOL = 1e-12


class SingularMatrixError(ValueError):
    """对奇异（不可逆）矩阵请求逆变换时抛出。"""


@dataclass(frozen=True)
class SingularReport:
    """奇异节点报告：记录不可逆发生的位置与几何处理方式。"""

    name: str
    det_value: float
    rank: int          # 线性部分秩：0 -> 坍缩为点；1 -> 坍缩为线
    handling: str      # 几何如何被处理


# ---------------------------------------------------------------------------
# 基本矩阵运算
# ---------------------------------------------------------------------------

def mat_mul(m1: Mat, m2: Mat) -> Mat:
    """返回 m1 @ m2（列向量约定下：先做 m2，再做 m1）。"""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def apply(m: Mat, x: float, y: float) -> tuple[float, float]:
    """把矩阵作用于单个点，返回 (x', y')。"""
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def det(m: Mat) -> float:
    """线性部分的行列式 a*d - b*c。"""
    a, b, c, d, _, _ = m
    return a * d - b * c


def rank2(m: Mat) -> int:
    """线性 2x2 部分的秩（0、1、2）。"""
    a, b, c, d, _, _ = m
    if a == 0.0 and b == 0.0 and c == 0.0 and d == 0.0:
        return 0
    if a * d - b * c == 0.0:
        return 1
    return 2


def is_singular(m: Mat, rtol: float = _SINGULAR_RTOL) -> bool:
    """是否不可逆。

    精确 det == 0 必判奇异；另用尺度自适应容差捕获“数值奇异”：
    |det| <= rtol * s^2，s 为线性部分最大元素绝对值。这样 scale(1e-200,...)
    多次复合导致的下溢也能被识别，而不会把接近单位阵的普通矩阵误报。
    """
    a, b, c, d, _, _ = m
    scale = max(abs(a), abs(b), abs(c), abs(d))
    if scale == 0.0:
        return True  # 线性部分全零，必然坍缩为点
    return abs(a * d - b * c) <= rtol * scale * scale


def mat_inv(m: Mat) -> Mat:
    """逆矩阵；奇异时抛 :class:`SingularMatrixError`（不产生 inf）。"""
    a, b, c, d, e, f = m
    dt = a * d - b * c
    if is_singular(m):
        raise SingularMatrixError(
            f"矩阵不可逆：det={dt!r}，秩={rank2(m)}；几何已坍缩，无法做逆映射"
        )
    ia, ib, ic, id_ = d / dt, -b / dt, -c / dt, a / dt
    ie = -(ia * e + ic * f)
    if_ = -(ib * e + id_ * f)
    return (ia, ib, ic, id_, ie, if_)


def rotation(theta: float) -> Mat:
    """绕原点逆时针旋转 theta（弧度）。"""
    c, s = math.cos(theta), math.sin(theta)
    return (c, s, -s, c, 0.0, 0.0)


def scale(sx: float, sy: float) -> Mat:
    """绕原点缩放（允许负值，负缩放等于镜像翻转）。"""
    return (sx, 0.0, 0.0, sy, 0.0, 0.0)


def translation(tx: float, ty: float) -> Mat:
    return (1.0, 0.0, 0.0, 1.0, tx, ty)


# ---------------------------------------------------------------------------
# 包围盒
# ---------------------------------------------------------------------------

def transform_bbox(m: Mat, box: BBox) -> BBox:
    """变换后的轴对齐包围盒。

    仿射映射把轴对齐矩形映为平行四边形；新 AABB 的每条边坐标是关于 (x,y)
    的线性函数加常数，线性函数在矩形上的极值必在顶点取得，因此只需变换 4 个
    角点再取极值即可精确（而非近似）得到结果，且对奇异矩阵同样有限、合法。
    """
    min_x, min_y, max_x, max_y = box
    pts = (
        apply(m, min_x, min_y),
        apply(m, max_x, min_y),
        apply(m, max_x, max_y),
        apply(m, min_x, max_y),
    )
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_union(b1: Optional[BBox], b2: Optional[BBox]) -> Optional[BBox]:
    if b1 is None:
        return b2
    if b2 is None:
        return b1
    return (
        min(b1[0], b2[0]),
        min(b1[1], b2[1]),
        max(b1[2], b2[2]),
        max(b1[3], b2[3]),
    )


def bbox_by_sampling(
    m: Mat,
    box: BBox,
    nx: int = 21,
    ny: int = 21,
    random_points: int = 0,
    rng: Optional[object] = None,
) -> BBox:
    """参考实现：逐点变换矩形内的网格点（+可选随机点）后取极值。

    用于对拍 :func:`transform_bbox`。随机点固定走标准库 random，保证可复现。
    """
    import random

    min_x, min_y, max_x, max_y = box
    xvals = [min_x + (max_x - min_x) * i / (nx - 1) for i in range(nx)]
    yvals = [min_y + (max_y - min_y) * j / (ny - 1) for j in range(ny)]
    gen = rng if rng is not None else random.Random(0)
    xs: list[float] = []
    ys: list[float] = []
    for x in xvals:
        for y in yvals:
            px, py = apply(m, x, y)
            xs.append(px)
            ys.append(py)
    for _ in range(random_points):
        px, py = apply(
            m, gen.uniform(min_x, max_x), gen.uniform(min_y, max_y)
        )
        xs.append(px)
        ys.append(py)
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------------
# 场景图
# ---------------------------------------------------------------------------

class Node:
    __slots__ = ("name", "local", "children", "local_bbox",
                 "world", "world_bbox", "degenerate")

    def __init__(
        self,
        name: str = "",
        local: Mat = IDENTITY,
        children: Iterable["Node"] = (),
        local_bbox: Optional[BBox] = None,
    ) -> None:
        self.name = name
        self.local = local
        self.children: list[Node] = list(children)
        self.local_bbox = local_bbox
        self.world: Optional[Mat] = None
        self.world_bbox: Optional[BBox] = None
        self.degenerate: bool = False


def compute_world_matrices(root: Node) -> list[SingularReport]:
    """自根向叶累积世界矩阵（显式栈迭代，深度无上限）。

    W_child = W_parent @ M_local。每个世界矩阵奇异的节点产生一条报告；
    若父节点已奇异，子节点直接继承退化标记，不重复报告（退化源头只报一次）。
    """
    reports: list[SingularReport] = []
    stack: list[tuple[Node, Mat, bool]] = [(root, IDENTITY, False)]
    while stack:
        node, parent_world, parent_degenerate = stack.pop()
        world = mat_mul(parent_world, node.local)
        node.world = world
        degenerate = parent_degenerate or is_singular(world)
        node.degenerate = degenerate
        if degenerate and not parent_degenerate:
            rk = rank2(world)
            reports.append(SingularReport(
                name=node.name,
                det_value=det(world),
                rank=rk,
                handling=(
                    "线性部分秩为0，几何坍缩为单个点；包围盒退化为该点，"
                    "禁止逆变换" if rk == 0 else
                    "线性部分秩为1，几何坍缩为线段（含平移）；包围盒按角点"
                    "变换取极值，结果有限，禁止逆变换"
                ),
            ))
        for child in node.children:
            stack.append((child, world, degenerate))
    return reports


def compute_world_bboxes(root: Node) -> Optional[BBox]:
    """用已合成的世界矩阵重算每个节点及整棵树的包围盒（显式栈迭代）。"""
    scene_bbox: Optional[BBox] = None
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.local_bbox is None or node.world is None:
            node.world_bbox = None
        else:
            wb = transform_bbox(node.world, node.local_bbox)
            if not all(math.isfinite(v) for v in wb):
                raise AssertionError(
                    f"节点 {node.name!r} 的包围盒出现非有限值，这是不允许的"
                )
            node.world_bbox = wb
            scene_bbox = bbox_union(scene_bbox, wb)
        stack.extend(node.children)
    return scene_bbox


def update_scene(root: Node) -> tuple[Optional[BBox], list[SingularReport]]:
    """一次调用完成矩阵合成 + 包围盒重算，返回（场景包围盒, 奇异报告）。"""
    reports = compute_world_matrices(root)
    scene_bbox = compute_world_bboxes(root)
    return scene_bbox, reports
