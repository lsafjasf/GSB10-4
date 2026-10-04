"""2D affine transform library (Python 3, standard library only).

Multiplication-order convention (mathematical, not empirical)
-------------------------------------------------------------
Points are COLUMN vectors. A transform M acts as  p' = M @ p.

Matrix layout (stored as a flat 6-tuple (a, b, c, d, e, f)):

    | a  c  e |       x' = a*x + c*y + e
    | b  d  f |       y' = b*x + d*y + f
    | 0  0  1 |

Composition: compose(outer, inner) = outer @ inner, i.e. `inner` is
applied to the point FIRST, `outer` SECOND. This is forced by the
column-vector convention:

    p' = inner @ p            (first transform)
    p'' = outer @ p' = outer @ (inner @ p) = (outer @ inner) @ p

Matrix multiplication is associative, so a whole ancestor chain can be
pre-multiplied into one matrix and applied once:

    world(node) = world(parent) @ local(node)
    world(root) = local(root)

Hence for a chain n0 -> n1 -> ... -> nk (n0 = root):

    world(nk) = L(n0) @ L(n1) @ ... @ L(nk)

and  p_world = world(nk) @ p_local  applies every ancestor transform in
the correct order with a single matrix-vector product.

Bounding-box correctness: an affine map sends a rectangle (a convex set)
to a parallelogram whose vertices are the images of the rectangle's
vertices. For a linear functional f(p) = x (or y), the maximum over a
convex polygon is attained at a vertex, so transforming the 4 corners
and taking min/max gives the EXACT axis-aligned bounding box -- no
sampling error is possible.

Singular matrices (det = a*d - b*c == 0): the map collapses the plane
onto a line or a point. Such a matrix has no inverse. Policy: the node
is REPORTED (id, path, determinant), its geometry is still transformed
(the image is a finite, degenerate segment/point, so its bbox is finite
and well-defined), children keep composing normally, and no inverse is
ever attempted. Nothing is silently skipped and no inf/nan is emitted.
"""

import math

EPS = 1e-12  # determinant threshold for singularity


# ---------------------------------------------------------------- constructors
def identity():
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def translation(tx, ty):
    return (1.0, 0.0, 0.0, 1.0, float(tx), float(ty))


def rotation(theta):
    """Counter-clockwise rotation by theta radians (right-handed frame)."""
    c, s = math.cos(theta), math.sin(theta)
    return (c, s, -s, c, 0.0, 0.0)


def scale(sx, sy):
    return (float(sx), 0.0, 0.0, float(sy), 0.0, 0.0)


# ---------------------------------------------------------------- core algebra
def compose(outer, inner):
    """Return outer @ inner (inner applied first). See module docstring."""
    ao, bo, co, do, eo, fo = outer
    ai, bi, ci, di, ei, fi = inner
    return (
        ao * ai + co * bi,
        bo * ai + do * bi,
        ao * ci + co * di,
        bo * ci + do * di,
        ao * ei + co * fi + eo,
        bo * ei + do * fi + fo,
    )


def apply(m, x, y):
    """Apply matrix m to point (x, y); returns (x', y')."""
    return (m[0] * x + m[2] * y + m[4],
            m[1] * x + m[3] * y + m[5])


def det(m):
    return m[0] * m[3] - m[1] * m[2]


def is_singular(m, eps=EPS):
    return abs(det(m)) <= eps


def inverse(m, eps=EPS):
    """Inverse of m. Raises ValueError for singular matrices -- callers
    must handle that case explicitly instead of getting inf/nan."""
    d = det(m)
    if abs(d) <= eps:
        raise ValueError("matrix is singular (det=%.3e), not invertible" % d)
    a, b, c, dd, e, f = m
    ia, ib, ic, idd = dd / d, -b / d, -c / d, a / d
    return (ia, ib, ic, idd, -(ia * e + ic * f), -(ib * e + idd * f))


def transform_bbox(m, bbox):
    """Exact AABB of an axis-aligned bbox under affine map m.

    bbox is (min_x, min_y, max_x, max_y). Correct because the extreme of
    a linear functional over a convex polygon is attained at a vertex.
    """
    min_x, min_y, max_x, max_y = bbox
    x0, y0 = apply(m, min_x, min_y)
    x1, y1 = apply(m, max_x, min_y)
    x2, y2 = apply(m, min_x, max_y)
    x3, y3 = apply(m, max_x, max_y)
    return (min(x0, x1, x2, x3), min(y0, y1, y2, y3),
            max(x0, x1, x2, x3), max(y0, y1, y2, y3))


# ---------------------------------------------------------------- scene graph
class Node:
    """Scene-graph node. `local` is this node's transform relative to its
    parent; `bbox` is the node's geometry bbox in its OWN local frame."""
    __slots__ = ("node_id", "local", "bbox", "children", "world")

    def __init__(self, node_id, local=None, bbox=None, children=()):
        self.node_id = node_id
        self.local = local if local is not None else identity()
        self.bbox = bbox
        self.children = list(children)
        self.world = None


def _singular_report(node, parent_of):
    # path is reconstructed only for reported nodes by walking the parent
    # map, so the common case pays O(1) per node (no string building --
    # concatenating path strings during traversal would make a depth-D
    # chain cost O(D^2)).
    ids = []
    cur = node
    while cur is not None:
        ids.append(str(cur.node_id))
        cur = parent_of.get(id(cur))
    return {
        "node_id": node.node_id,
        "path": "/".join(reversed(ids)),
        "det": det(node.local),
        "policy": ("non-invertible: geometry collapses to a "
                   "line/point; bbox still computed (finite, "
                   "degenerate); children composed normally; "
                   "no inverse attempted"),
    }


def compute_world(root):
    """Iteratively compose world matrices for the whole tree.

    Fully iterative (explicit stack) so arbitrarily deep chains cannot
    overflow the Python recursion limit. Returns a list of singular-node
    reports: dicts with node_id, path, det and the handling policy.
    """
    reports = []
    parent_of = {id(root): None}
    # stack entries: (node, parent_world)
    stack = [(root, identity())]
    while stack:
        node, parent_world = stack.pop()
        world = compose(parent_world, node.local)
        node.world = world
        if is_singular(node.local):
            reports.append(_singular_report(node, parent_of))
        for child in node.children:
            parent_of[id(child)] = node
            stack.append((child, world))
    return reports


def compute_bboxes(root):
    """Iteratively recompute each node's world-space bbox from its
    composed world matrix. Returns (tree_bbox, singular_reports).
    tree_bbox is the union of all node bboxes (None if tree has none)."""
    reports = []
    tree_bbox = None
    parent_of = {id(root): None}
    stack = [(root, identity())]
    while stack:
        node, parent_world = stack.pop()
        world = compose(parent_world, node.local)
        node.world = world
        if is_singular(node.local):
            reports.append(_singular_report(node, parent_of))
        if node.bbox is not None:
            wb = transform_bbox(world, node.bbox)
            if tree_bbox is None:
                tree_bbox = wb
            else:
                tree_bbox = (min(tree_bbox[0], wb[0]), min(tree_bbox[1], wb[1]),
                             max(tree_bbox[2], wb[2]), max(tree_bbox[3], wb[3]))
        for child in node.children:
            parent_of[id(child)] = node
            stack.append((child, world))
    return tree_bbox, reports
