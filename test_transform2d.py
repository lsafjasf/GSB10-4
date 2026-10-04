"""test_transform2d.py — transform2d 的自测（标准库 unittest）。

覆盖：单位矩阵、只旋转、只缩放（含负缩放）、乘序约定、父子叠加、
奇异矩阵报告与几何处理、逆矩阵报错、包围盒角点法 vs 逐点法对拍、
超深变换链、大量兄弟节点、结果有限性（无 inf/nan）。

运行：python3 test_transform2d.py -v
"""
import math
import random
import unittest

from transform2d import (
    IDENTITY, BBox, Mat, Node, SingularMatrixError,
    mat_mul, mat_inv, apply, det, is_singular, rank2,
    rotation, scale, translation,
    transform_bbox, bbox_by_sampling, bbox_union,
    compute_world_matrices, compute_world_bboxes, update_scene,
)

UNIT_BOX = (-1.0, -2.0, 3.0, 4.0)


def bbox_close(b1: BBox, b2: BBox, tol: float = 1e-9) -> bool:
    scale = max(1.0, *(abs(v) for v in (*b1, *b2)))
    return all(abs(x - y) <= tol * scale for x, y in zip(b1, b2))


def mat_close(m1: Mat, m2: Mat, tol: float = 1e-9) -> bool:
    scale = max(1.0, *(abs(v) for v in (*m1, *m2)))
    return all(abs(x - y) <= tol * scale for x, y in zip(m1, m2))


class TestBasicMatrices(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(rotation(0.0), IDENTITY)
        self.assertEqual(mat_mul(IDENTITY, IDENTITY), IDENTITY)
        x, y = apply(IDENTITY, 7.0, -9.0)
        self.assertEqual((x, y), (7.0, -9.0))
        self.assertEqual(transform_bbox(IDENTITY, UNIT_BOX), UNIT_BOX)
        # 单位矩阵是所有矩阵的乘法单位元
        m = mat_mul(rotation(0.3), translation(5.0, -2.0))
        self.assertEqual(mat_mul(IDENTITY, m), m)
        self.assertEqual(mat_mul(m, IDENTITY), m)

    def test_rotation_only(self):
        r90 = rotation(math.pi / 2)
        # (1, 0) -> (0, 1)，(0, 1) -> (-1, 0)
        self.assertTrue(mat_close(apply(r90, 1.0, 0.0), (0.0, 1.0)))
        self.assertTrue(mat_close(apply(r90, 0.0, 1.0), (-1.0, 0.0)))
        # 对称盒 (-1,-1)-(1,1) 旋转 90° 后包围盒不变
        self.assertTrue(bbox_close(
            transform_bbox(r90, (-1, -1, 1, 1)), (-1, -1, 1, 1)))
        # 非对称盒 (-1,-2)-(3,4) 旋转 90°：(x,y)->(-y,x)，手算 x∈[-4,2], y∈[-1,3]
        got = transform_bbox(r90, (-1, -2, 3, 4))
        self.assertTrue(bbox_close(got, (-4, -1, 2, 3)), got)
        self.assertEqual(rank2(r90), 2)
        self.assertFalse(is_singular(r90))

    def test_scale_only(self):
        s = scale(2.0, -3.0)  # 负缩放=镜像
        self.assertEqual(apply(s, 1.0, 1.0), (2.0, -3.0))
        # 极值会随镜像交换，角点法必须仍然给出正确 min/max
        got = transform_bbox(s, (-1, -2, 3, 4))
        self.assertTrue(bbox_close(got, (-2, -12, 6, 6)), got)
        # 只缩放节点：世界矩阵就是缩放本身
        node = Node("s", scale(4.0, 0.5), local_bbox=UNIT_BOX)
        reports = compute_world_matrices(node)
        self.assertEqual(reports, [])
        self.assertTrue(bbox_close(
            compute_world_bboxes(node), (-4, -1, 12, 2)))

    def test_inverse_basic(self):
        m = mat_mul(translation(3.0, -4.0), rotation(0.7))
        inv = mat_inv(m)
        for p in ((0.0, 0.0), (1.5, -2.2), (-3.0, 7.0)):
            q = apply(m, *p)
            back = apply(inv, *q)
            self.assertTrue(mat_close(back, p, 1e-10))


class TestMultiplicationOrder(unittest.TestCase):
    """乘序不是试出来的：手算两组不可交换的复合并核对。"""

    def test_rotate_then_scale(self):
        r90 = rotation(math.pi / 2)
        s = scale(2.0, 3.0)
        p = (1.0, 0.0)
        # “先旋转后缩放” = S @ R：R(1,0)=(0,1)，S->(0,3)
        self.assertTrue(mat_close(apply(mat_mul(s, r90), *p), (0.0, 3.0)))
        # “先缩放后旋转” = R @ S：S(1,0)=(2,0)，R->(0,2)
        self.assertTrue(mat_close(apply(mat_mul(r90, s), *p), (0.0, 2.0)))
        self.assertNotEqual(mat_mul(s, r90), mat_mul(r90, s))

    def test_translate_then_rotate(self):
        r90 = rotation(math.pi / 2)
        t = translation(10.0, 0.0)
        p = (1.0, 0.0)
        # 先旋转后平移 T @ R：(0,1) -> (10,1)
        self.assertTrue(mat_close(apply(mat_mul(t, r90), *p), (10.0, 1.0)))
        # 先平移后旋转 R @ T：(11,0) -> (0,11)
        self.assertTrue(mat_close(apply(mat_mul(r90, t), *p), (0.0, 11.0)))

    def test_parent_child_accumulation(self):
        # 父旋转 90°，子平移 (10,0)；世界点 = R@T 作用于局部点
        child = Node("child", translation(10.0, 0.0))
        root = Node("root", rotation(math.pi / 2), children=[child])
        compute_world_matrices(root)
        self.assertTrue(mat_close(
            apply(child.world, 1.0, 0.0), (0.0, 11.0)))
        self.assertTrue(mat_close(child.world, mat_mul(root.local, child.local)))


class TestSingular(unittest.TestCase):
    def test_zero_scale_collapses_axis(self):
        # sx=0：平面坍缩到 y 轴方向的线段
        node = Node("flat", scale(0.0, 5.0), local_bbox=(0, 0, 10, 10))
        reports = compute_world_matrices(node)
        box = compute_world_bboxes(node)
        self.assertEqual(len(reports), 1)
        rep = reports[0]
        self.assertEqual(rep.name, "flat")
        self.assertEqual(rep.det_value, 0.0)
        self.assertEqual(rep.rank, 1)
        self.assertIn("坍缩为线段", rep.handling)
        # 所有 x 坍缩为 0，y 方向仍被放大 5 倍：有限、零宽度
        self.assertTrue(bbox_close(box, (0, 0, 0, 50)), box)
        self.assertTrue(all(math.isfinite(v) for v in box))
        self.assertTrue(node.degenerate)
        with self.assertRaises(SingularMatrixError):
            mat_inv(node.world)

    def test_rank_zero_collapses_to_point(self):
        # 线性部分全零 + 平移：整个平面坍缩为点 (3, 4)
        m = (0.0, 0.0, 0.0, 0.0, 3.0, 4.0)
        node = Node("point", m, local_bbox=(-10, -10, 10, 10))
        reports = compute_world_matrices(node)
        box = compute_world_bboxes(node)
        self.assertEqual(reports[0].rank, 0)
        self.assertTrue(bbox_close(box, (3, 4, 3, 4)), box)

    def test_singular_ancestor_propagates_once(self):
        # 父奇异 -> 子孙几何同样坍缩；源头只报告一次，子孙不静默跳过
        leaf = Node("leaf", translation(2.0, 2.0),
                    local_bbox=(0, 0, 1, 1))
        mid = Node("mid", rotation(0.5), children=[leaf])
        root = Node("root", scale(0.0, 1.0), children=[mid])
        reports = compute_world_matrices(root)
        box = compute_world_bboxes(root)
        self.assertEqual([r.name for r in reports], ["root"])
        self.assertTrue(mid.degenerate and leaf.degenerate)
        # S(0,1)@R(0.5)@T(2,2)：坍缩到 x=0 的线段，y∈[2(s+c), 3(s+c)]
        s_, c_ = math.sin(0.5), math.cos(0.5)
        self.assertTrue(bbox_close(box, (0, 2*(s_+c_), 0, 3*(s_+c_))), box)
        self.assertTrue(all(math.isfinite(v) for v in box))

    def test_numerically_singular_detected(self):
        tiny = scale(1e-200, 1e-200)
        m = mat_mul(scale(1e-200, 1.0), tiny)  # det 下溢为 0
        self.assertTrue(is_singular(m))


class TestBBoxDifferential(unittest.TestCase):
    """角点法 vs 逐点网格+随机点法 对拍。"""

    def cases(self):
        return [
            IDENTITY,
            rotation(math.pi / 2),
            rotation(0.37),
            scale(2.0, -3.0),
            scale(-0.5, 0.25),
            mat_mul(translation(100, -50), rotation(0.3)),
            mat_mul(rotation(-1.1), scale(4.0, 0.1)),
        ]

    def test_corner_versus_sampling_fixed(self):
        box = (-3.0, -2.0, 5.0, 7.0)
        for i, m in enumerate(self.cases()):
            corner = transform_bbox(m, box)
            sampled = bbox_by_sampling(m, box, 101, 101,
                                       random_points=5000,
                                       rng=random.Random(i + 1))
            self.assertTrue(bbox_close(corner, sampled, 1e-9),
                            (i, corner, sampled))
            # 线性泛函极值在角点：采样盒不应比角点盒大（仅容浮点误差）
            s = max(1.0, *(abs(v) for v in corner))
            self.assertGreaterEqual(sampled[0], corner[0] - 1e-9 * s)
            self.assertLessEqual(sampled[2], corner[2] + 1e-9 * s)

    def test_corner_versus_sampling_random(self):
        rng = random.Random(20261005)
        for _ in range(200):
            m = (rng.uniform(-5, 5), rng.uniform(-5, 5),
                 rng.uniform(-5, 5), rng.uniform(-5, 5),
                 rng.uniform(-20, 20), rng.uniform(-20, 20))
            box = (rng.uniform(-10, 0), rng.uniform(-10, 0),
                   rng.uniform(0, 10), rng.uniform(0, 10))
            corner = transform_bbox(m, box)
            sampled = bbox_by_sampling(m, box, 33, 33,
                                       random_points=200, rng=rng)
            self.assertTrue(bbox_close(corner, sampled, 1e-8),
                            (m, box, corner, sampled))
            self.assertTrue(all(math.isfinite(v) for v in corner))


class TestScale(unittest.TestCase):
    def test_deep_chain(self):
        depth = 200_000  # 远超默认递归上限 1000
        angle = 2.0 * math.pi / depth
        leaf = Node("n0", rotation(angle))
        node = leaf
        for i in range(1, depth):
            node = Node(f"n{i}", rotation(angle), children=[node])
        reports = compute_world_matrices(node)
        self.assertEqual(reports, [])
        # depth 个小旋转复合 = 旋转 2π ≈ 单位阵（正交复合误差有界）
        self.assertTrue(mat_close(leaf.world, IDENTITY, 1e-6),
                        leaf.world)
        # 深链上的包围盒重算同样不溢出且有限
        leaf.local_bbox = (0, 0, 1, 1)
        box = compute_world_bboxes(node)
        self.assertTrue(all(math.isfinite(v) for v in box))
        # 总旋转≈2π≈单位阵，盒 (0,0)-(1,1) 的包围盒≈自身
        self.assertTrue(bbox_close(box, (0, 0, 1, 1), 1e-5))

    def test_many_siblings(self):
        n = 100_000
        children = [
            Node(f"s{i}", translation(float(i), float(-i)),
                 local_bbox=(-1, -1, 1, 1))
            for i in range(n)
        ]
        root = Node("root", scale(2.0, 1.0), children=children)
        reports = compute_world_matrices(root)
        box = compute_world_bboxes(root)
        self.assertEqual(reports, [])
        # 子节点世界 x=2i，盒半宽 2；世界 y=-i，盒半高 1
        self.assertTrue(bbox_close(box, (-2, -(n - 1) - 1,
                                         2 * (n - 1) + 2, 1), 1e-6), box)

    def test_update_scene_reports_and_finite(self):
        weird = Node("zero", scale(0.0, 2.0), local_bbox=(0, 0, 5, 5))
        root = Node("root", rotation(0.25),
                    children=[weird, Node("ok", scale(3, 3),
                                          local_bbox=(0, 0, 1, 1))])
        box, reports = update_scene(root)
        self.assertEqual(len(reports), 1)
        self.assertFalse(any(math.isinf(v) or math.isnan(v) for v in box))


if __name__ == "__main__":
    unittest.main(verbosity=2)
