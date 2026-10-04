"""Self-tests for transform2d (stdlib unittest only).

Cross-check ("对拍") methodology: for random trees with random
transforms, every sample point is transformed two independent ways --
  (A) composed:  p_world = world(node) @ p          (one matrix)
  (B) stepwise:  apply each ancestor's local matrix in order along the
      path from the root                          (no composition)
The two must agree to float round-off. Bboxes are cross-checked the same
way: bbox from the composed matrix vs extremes of stepwise-transformed
points (corners plus dense interior samples).
"""

import math
import random
import sys
import unittest

import transform2d as T


def stepwise_apply(path_locals, x, y):
    """Independent reference: apply local matrices one by one.

    path_locals is [L_root, ..., L_node]. A point in the node's frame is
    carried to the world frame by applying the node's OWN local first,
    then its parent's, ..., then the root's -- i.e. reversed(path_locals).
    This matches world = L_root @ ... @ L_node acting on a column vector
    (rightmost matrix acts first).
    """
    for m in reversed(path_locals):
        x, y = T.apply(m, x, y)
    return x, y


def collect_paths(root):
    """Iteratively yield (node, [local matrices root..node])."""
    out = []
    stack = [(root, [root.local])]
    while stack:
        node, locals_ = stack.pop()
        out.append((node, locals_))
        for c in node.children:
            stack.append((c, locals_ + [c.local]))
    return out


class TestConventions(unittest.TestCase):
    def test_identity(self):
        m = T.identity()
        self.assertEqual(T.apply(m, 3.0, -7.5), (3.0, -7.5))
        self.assertEqual(T.compose(m, m), m)
        r = T.rotation(0.7)
        self.assertTrue(all(abs(a - b) < 1e-15
                            for a, b in zip(T.compose(T.identity(), r), r)))
        self.assertTrue(all(abs(a - b) < 1e-15
                            for a, b in zip(T.compose(r, T.identity()), r)))

    def test_rotation_only(self):
        r = T.rotation(math.pi / 2)
        x, y = T.apply(r, 1.0, 0.0)
        self.assertAlmostEqual(x, 0.0, places=12)
        self.assertAlmostEqual(y, 1.0, places=12)
        # pure rotation preserves length and has det == 1
        x, y = T.apply(T.rotation(1.234), 3.0, 4.0)
        self.assertAlmostEqual(math.hypot(x, y), 5.0, places=12)
        self.assertAlmostEqual(T.det(T.rotation(2.0)), 1.0, places=12)

    def test_scale_only(self):
        s = T.scale(2.0, 3.0)
        self.assertEqual(T.apply(s, 1.5, -2.0), (3.0, -6.0))
        self.assertAlmostEqual(T.det(s), 6.0)

    def test_order_matters(self):
        # rotate-then-scale (S @ R) vs scale-then-rotate (R @ S):
        # matrix multiplication is non-commutative; verify numerically
        # against the stepwise reference, not by "trying values".
        r = T.rotation(math.pi / 2)
        s = T.scale(2.0, 1.0)
        p = (1.0, 0.5)
        # S @ R means: rotate first, then scale
        m1 = T.compose(s, r)
        ref1 = T.apply(s, *T.apply(r, *p))
        self.assertEqual(T.apply(m1, *p), ref1)
        # R @ S means: scale first, then rotate
        m2 = T.compose(r, s)
        ref2 = T.apply(r, *T.apply(s, *p))
        self.assertEqual(T.apply(m2, *p), ref2)
        # and the two orders genuinely differ
        self.assertGreater(abs(m1[0] - m2[0]) + abs(m1[1] - m2[1])
                           + abs(m1[2] - m2[2]) + abs(m1[3] - m2[3]), 0.5)

    def test_inverse_roundtrip_and_singular_raises(self):
        m = T.compose(T.translation(5, -3),
                      T.compose(T.rotation(0.9), T.scale(2, 0.5)))
        inv = T.inverse(m)
        p = (1.25, -4.75)
        q = T.apply(inv, *T.apply(m, *p))
        self.assertAlmostEqual(q[0], p[0], places=10)
        self.assertAlmostEqual(q[1], p[1], places=10)
        with self.assertRaises(ValueError):
            T.inverse(T.scale(1.0, 0.0))


class TestCrossCheck(unittest.TestCase):
    """对拍: composed matrix vs stepwise application, random trees."""

    def test_random_tree_points_and_bboxes(self):
        rng = random.Random(20261005)
        table_rows = []
        worst_point_err = 0.0
        worst_bbox_err = 0.0

        for case in range(8):
            # random tree: depth <= 6, up to 4 children per node
            def build(depth):
                local = T.compose(
                    T.translation(rng.uniform(-50, 50), rng.uniform(-50, 50)),
                    T.compose(T.rotation(rng.uniform(-math.pi, math.pi)),
                              T.scale(rng.uniform(0.2, 3.0),
                                      rng.uniform(0.2, 3.0))))
                bbox = (rng.uniform(-10, 0), rng.uniform(-10, 0),
                        rng.uniform(0, 10), rng.uniform(0, 10))
                kids = [] if depth >= 6 else [build(depth + 1)
                                              for _ in range(rng.randint(0, 4))]
                return T.Node("n%d_%d" % (case, depth), local, bbox, kids)

            root = build(0)
            T.compute_world(root)
            paths = collect_paths(root)

            # (1) point cross-check on every node of the tree
            for node, locals_ in paths:
                for _ in range(5):
                    px, py = rng.uniform(-10, 10), rng.uniform(-10, 10)
                    ax, ay = T.apply(node.world, px, py)
                    bx, by = stepwise_apply(locals_, px, py)
                    worst_point_err = max(worst_point_err,
                                          abs(ax - bx), abs(ay - by))

            # (2) bbox cross-check on every node: composed-matrix bbox vs
            # extremes of stepwise-transformed corners + interior samples
            for node, locals_ in paths:
                mnx, mny, mxx, mxy = node.bbox
                pts = [(mnx, mny), (mxx, mny), (mnx, mxy), (mxx, mxy)]
                pts += [(rng.uniform(mnx, mxx), rng.uniform(mny, mxy))
                        for _ in range(200)]
                sw = [stepwise_apply(locals_, px, py) for px, py in pts]
                bbox_step = (min(p[0] for p in sw), min(p[1] for p in sw),
                             max(p[0] for p in sw), max(p[1] for p in sw))
                bbox_comp = T.transform_bbox(node.world, node.bbox)
                # composed bbox must CONTAIN sampled extremes and match
                # corner extremes exactly (corners are in the sample set)
                err = max(abs(bbox_comp[i] - bbox_step[i]) for i in range(4))
                worst_bbox_err = max(worst_bbox_err, err)
                if case < 4 and node is root:
                    table_rows.append((case, bbox_comp, bbox_step, err))

        print("\n=== 对拍数据: composed-matrix bbox vs stepwise point extremes ===")
        print("case | bbox (composed matrix)            | "
              "bbox (point-by-point extremes)      | max|diff|")
        for case, bc, bs, err in table_rows:
            fmt = lambda b: "(%8.3f,%8.3f,%8.3f,%8.3f)" % b
            print("  %d  | %s | %s | %.3e" % (case, fmt(bc), fmt(bs), err))
        print("worst point error over all nodes/samples: %.3e" % worst_point_err)
        print("worst bbox  error over all nodes        : %.3e" % worst_bbox_err)

        self.assertLess(worst_point_err, 1e-8)
        self.assertLess(worst_bbox_err, 1e-8)


class TestSingular(unittest.TestCase):
    def test_singular_reported_and_finite(self):
        # scale(sx, 0) collapses the plane onto the x-axis: det == 0
        leaf = T.Node("leaf", T.translation(1, 1), (0, 0, 2, 2))
        squashed = T.Node("squash", T.scale(3.0, 0.0), (0, 0, 4, 4),
                          children=[leaf])
        normal = T.Node("normal", T.rotation(0.3), (0, 0, 1, 1))
        root = T.Node("root", T.translation(10, 20),
                      children=[squashed, normal])

        tree_bbox, reports = T.compute_bboxes(root)

        print("\n=== 奇异矩阵报告 ===")
        for r in reports:
            print("node=%s path=%s det=%.3e" % (r["node_id"], r["path"], r["det"]))
            print("  policy: %s" % r["policy"])

        # exactly one singular node, identified by id and path
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["node_id"], "squash")
        self.assertEqual(reports[0]["det"], 0.0)

        # nothing silently skipped: every node got a world matrix,
        # every output is finite (no inf / nan)
        for node, _ in collect_paths(root):
            self.assertIsNotNone(node.world)
            self.assertTrue(all(math.isfinite(v) for v in node.world))
        self.assertTrue(all(math.isfinite(v) for v in tree_bbox))

        # degenerate geometry: squashed node's world bbox has zero height
        # (plane collapsed onto a line by scale(*, 0))
        wb = T.transform_bbox(squashed.world, squashed.bbox)
        self.assertAlmostEqual(wb[3] - wb[1], 0.0, places=12)
        # child of a singular node is still composed, not skipped
        self.assertIsNotNone(leaf.world)
        self.assertTrue(all(math.isfinite(v) for v in leaf.world))


class TestScaleLimits(unittest.TestCase):
    def test_super_deep_chain_no_recursion_overflow(self):
        depth = 200_000  # far beyond Python's default recursion limit (1000)
        self.assertLess(1000, depth)
        self.assertEqual(sys.getrecursionlimit(), 1000)
        root = cur = T.Node(0, T.translation(1.0, -0.5))
        for i in range(1, depth):
            nxt = T.Node(i, T.translation(1.0, -0.5))
            cur.children.append(nxt)
            cur = nxt
        reports = T.compute_world(root)
        self.assertEqual(reports, [])
        # each level adds (1, -0.5): leaf world must be translation by
        # depth * (1, -0.5); verify against closed form, not by trying
        x, y = T.apply(cur.world, 0.0, 0.0)
        self.assertAlmostEqual(x, float(depth), places=6)
        self.assertAlmostEqual(y, -0.5 * depth, places=6)
        self.assertTrue(all(math.isfinite(v) for v in cur.world))

    def test_many_siblings(self):
        n = 100_000
        root = T.Node("root", T.identity(),
                      children=[T.Node(i, T.translation(i, -i))
                                for i in range(n)])
        reports = T.compute_world(root)
        self.assertEqual(reports, [])
        last = root.children[-1]
        self.assertEqual(T.apply(last.world, 0, 0),
                         (float(n - 1), float(-(n - 1))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
