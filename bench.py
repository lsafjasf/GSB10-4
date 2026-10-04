"""bench.py — 10 万节点场景的矩阵合成与包围盒重算计时。

场景：一条 5 万节点深的变换链 + 根下 5 万个兄弟节点（共 100001 个节点），
每个节点都有局部包围盒。遍历为显式栈迭代。运行：python3 bench.py
"""
import math
import random
import time

from transform2d import (
    Node, rotation, scale, translation, mat_mul,
    compute_world_matrices, compute_world_bboxes,
)

CHAIN = 50_000
SIBLINGS = 50_000
TOTAL = CHAIN + SIBLINGS + 1


def build_scene() -> Node:
    rng = random.Random(42)
    angle = 2 * math.pi / CHAIN
    chain_local = mat_mul(rotation(angle), scale(1.0 + 1e-9, 1.0 - 1e-9))
    leaf = Node("chain_tip", chain_local, local_bbox=(-1, -1, 1, 1))
    node = leaf
    for i in range(CHAIN - 1):
        node = Node(f"c{i}", chain_local, children=[node],
                    local_bbox=(-1, -1, 1, 1))
    siblings = [
        Node(f"s{i}",
             mat_mul(translation(float(i), float(-i)),
                     rotation(rng.uniform(-0.1, 0.1))),
             local_bbox=(-1, -1, 1, 1))
        for i in range(SIBLINGS)
    ]
    return Node("root", scale(2.0, 1.0), children=[node, *siblings],
                local_bbox=(-1, -1, 1, 1))


def main() -> None:
    print(f"构建 {TOTAL} 节点场景（深链 {CHAIN} + 兄弟 {SIBLINGS}）...")
    t0 = time.perf_counter()
    root = build_scene()
    print(f"构建耗时 {time.perf_counter() - t0:.3f} s")

    t0 = time.perf_counter()
    reports = compute_world_matrices(root)
    t_compose = time.perf_counter() - t0

    t0 = time.perf_counter()
    scene_bbox = compute_world_bboxes(root)
    t_bbox = time.perf_counter() - t0

    assert all(math.isfinite(v) for v in scene_bbox)
    print(f"矩阵合成   : {t_compose:.3f} s "
          f"({t_compose * 1e6 / TOTAL:.2f} us/节点)")
    print(f"包围盒重算 : {t_bbox:.3f} s "
          f"({t_bbox * 1e6 / TOTAL:.2f} us/节点)")
    print(f"合计       : {t_compose + t_bbox:.3f} s")
    print(f"场景包围盒 : {tuple(round(v, 3) for v in scene_bbox)}")
    print(f"奇异报告   : {len(reports)} 条")


if __name__ == "__main__":
    main()
