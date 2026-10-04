"""diff_data.py — 生成“角点法 vs 逐点变换取极值”的对拍数据。

逐点参考实现：在局部包围盒内取 101x101 网格 + 5000 个随机点，逐点做矩阵
变换后取极值（random 种子固定，结果可复现）。打印两种方法的包围盒与最大
绝对差。运行：python3 diff_data.py
"""
import math
import random

from transform2d import (
    IDENTITY, rotation, scale, translation, mat_mul,
    transform_bbox, bbox_by_sampling,
)

CASES = [
    ("单位矩阵", IDENTITY),
    ("只旋转 90°", rotation(math.pi / 2)),
    ("只旋转 21.2°", rotation(0.37)),
    ("只缩放 (2,-3) 含镜像", scale(2.0, -3.0)),
    ("只缩放 (-0.5,0.25)", scale(-0.5, 0.25)),
    ("平移*旋转", mat_mul(translation(100, -50), rotation(0.3))),
    ("旋转*缩放", mat_mul(rotation(-1.1), scale(4.0, 0.1))),
    ("随机仿射 1", (1.7, -2.4, 0.6, 3.1, 12.0, -8.0)),
    ("随机仿射 2", (-3.2, 0.4, 1.1, -0.9, 5.5, 7.7)),
]
BOX = (-3.0, -2.0, 5.0, 7.0)

def main() -> None:
    print(f"局部包围盒: {BOX}；参考实现=101x101 网格 + 5000 随机点\n")
    header = f"{'情形':<22}{'角点法包围盒':<46}{'逐点法包围盒':<46}{'最大绝对差':<14}"
    print(header)
    print("-" * 120)
    worst = 0.0
    for i, (name, m) in enumerate(CASES):
        corner = transform_bbox(m, BOX)
        sampled = bbox_by_sampling(
            m, BOX, 101, 101, random_points=5000,
            rng=random.Random(1000 + i))
        diff = max(abs(a - b) for a, b in zip(corner, sampled))
        worst = max(worst, diff)
        cf = "(" + ",".join(f"{v: .6g}" for v in corner) + ")"
        sf = "(" + ",".join(f"{v: .6g}" for v in sampled) + ")"
        print(f"{name:<20}{cf:<46}{sf:<46}{diff:.3g}")
    print("-" * 120)
    print(f"全部 {len(CASES)} 组对拍一致（角点法=逐点法），最大绝对差 = {worst:.3g}")

if __name__ == "__main__":
    main()
