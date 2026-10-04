"""Benchmark: 100,000 nodes -- world-matrix composition + bbox recompute.

Tree shape: depth 100 chain, every chain node has 999 siblings at level
1 style layout => exactly 100,000 nodes (100 chain nodes +
999 per chain node... see construction below). Also reports a worst-case
pure-chain variant for reference. Standard library only.
"""

import math
import random
import time

import transform2d as T


def build_tree(total=100_000, seed=42):
    """Depth-100 spine; each spine node carries a fan of siblings so the
    tree has exactly `total` nodes: 100 spine nodes + 100 groups of
    leaf siblings. 100 + 100*k = total -> k = 999 siblings each."""
    rng = random.Random(seed)

    def rand_local():
        return T.compose(
            T.translation(rng.uniform(-10, 10), rng.uniform(-10, 10)),
            T.compose(T.rotation(rng.uniform(-0.5, 0.5)),
                      T.scale(rng.uniform(0.5, 1.5),
                              rng.uniform(0.5, 1.5))))

    def bbox():
        return (rng.uniform(-2, 0), rng.uniform(-2, 0),
                rng.uniform(0, 2), rng.uniform(0, 2))

    spine = 100
    per_level = (total - spine) // spine  # 999
    root = T.Node(0, rand_local(), bbox())
    cur = root
    count = 1
    for j in range(per_level):
        root.children.append(T.Node("leaf-0-%d" % j, rand_local(), bbox()))
        count += 1
    for d in range(1, spine):
        node = T.Node(d, rand_local(), bbox())
        cur.children.append(node)
        cur = node
        count += 1
        for j in range(per_level):
            node.children.append(
                T.Node("leaf-%d-%d" % (d, j), rand_local(), bbox()))
            count += 1
    return root, count


def build_chain(n):
    root = cur = T.Node(0, T.translation(1.0, 0.0), (0, 0, 1, 1))
    for i in range(1, n):
        nxt = T.Node(i, T.translation(1.0, 0.0), (0, 0, 1, 1))
        cur.children.append(nxt)
        cur = nxt
    return root


def main():
    root, count = build_tree(100_000)
    assert count == 100_000, count

    t0 = time.perf_counter()
    reports = T.compute_world(root)
    t1 = time.perf_counter()
    tree_bbox, reports2 = T.compute_bboxes(root)
    t2 = time.perf_counter()

    print("=== 100,000-node tree (spine depth 100, 999 siblings/level) ===")
    print("composition only   : %8.1f ms  (%.2f us/node)" %
          ((t1 - t0) * 1e3, (t1 - t0) * 1e6 / count))
    print("compose + bbox rec.: %8.1f ms  (%.2f us/node)" %
          ((t2 - t0) * 1e3, (t2 - t0) * 1e6 / count))
    print("singular reports: %d" % len(reports))
    assert all(math.isfinite(v) for v in tree_bbox)
    print("tree bbox: (%.3f, %.3f, %.3f, %.3f)" % tree_bbox)

    chain = build_chain(100_000)
    t0 = time.perf_counter()
    bbox, rep = T.compute_bboxes(chain)
    dt = time.perf_counter() - t0
    print("\n=== 100,000-node pure chain (worst-case depth) ===")
    print("compose + bbox rec.: %8.1f ms  (%.2f us/node), singular=%d" %
          (dt * 1e3, dt * 1e6 / 100_000, len(rep)))
    print("bbox max_x: %.1f (tip translation 100000 + local bbox width 1)" % bbox[2])


if __name__ == "__main__":
    main()
