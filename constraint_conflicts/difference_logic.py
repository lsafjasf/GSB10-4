"""Difference-constraint systems and negative-cycle based conflict extraction.

A constraint ``x <= y + c`` is represented as a directed edge ``y -> x``
with weight ``c``.  The system is satisfiable iff the graph has no
negative-weight cycle.  A simple negative cycle is exactly an
inclusion-minimal unsatisfiable subset (MUS): removing any one of its
edges breaks the only cycle, and an acyclic difference-constraint graph
is always satisfiable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class DifferenceConstraint:
    """``upper_var - lower_var <= bound`` i.e. ``lower <= upper + bound``."""

    id: str
    lower: str
    upper: str
    bound: int

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("constraint id must be non-empty")
        if not self.lower or not self.upper:
            raise ValueError("variable names must be non-empty")
        if isinstance(self.bound, bool) or not isinstance(self.bound, int):
            raise TypeError("bound must be an int")


# Edge: (source_vertex, target_vertex, weight, constraint_index)
Edge = Tuple[int, int, int, int]


class DifferenceSystem:
    """A finite set of difference constraints compiled to a weighted digraph."""

    def __init__(self, constraints: Iterable[DifferenceConstraint]):
        self.constraints: Tuple[DifferenceConstraint, ...] = tuple(constraints)
        ids = [c.id for c in self.constraints]
        if len(set(ids)) != len(ids):
            raise ValueError("constraint ids must be unique")

        var_ids: Dict[str, int] = {}
        variables: List[str] = []

        def vid(name: str) -> int:
            if name not in var_ids:
                var_ids[name] = len(variables)
                variables.append(name)
            return var_ids[name]

        edges: List[Edge] = []
        for index, c in enumerate(self.constraints):
            # lower <= upper + bound  =>  edge upper -> lower with weight bound
            edges.append((vid(c.upper), vid(c.lower), c.bound, index))

        self.variables: Tuple[str, ...] = tuple(variables)
        self._var_ids = var_ids
        self.edges: Tuple[Edge, ...] = tuple(edges)
        adjacency: List[List[int]] = [[] for _ in variables]
        for edge_index, (src, _dst, _w, _ci) in enumerate(self.edges):
            adjacency[src].append(edge_index)
        self.adjacency: Tuple[Tuple[int, ...], ...] = tuple(tuple(a) for a in adjacency)

    @property
    def variable_count(self) -> int:
        return len(self.variables)

    @property
    def constraint_count(self) -> int:
        return len(self.constraints)


def _extract_cycle(
    system: DifferenceSystem,
    pred_vertex: Sequence[int],
    pred_edge: Sequence[int],
    changed: int,
) -> Tuple[Tuple[int, ...], int]:
    n = system.variable_count
    y = changed
    for _ in range(n):
        y = pred_vertex[y]
        if y < 0:
            raise RuntimeError("predecessor chain broke before reaching a cycle")

    seen: Dict[int, int] = {}
    order: List[int] = []
    cur = y
    while cur not in seen:
        seen[cur] = len(order)
        order.append(cur)
        cur = pred_vertex[cur]
        if cur < 0:
            raise RuntimeError("predecessor chain broke while extracting a cycle")
    cycle_vertices = order[seen[cur]:]

    edge_indices = [pred_edge[v] for v in cycle_vertices]
    if any(e < 0 for e in edge_indices):
        raise RuntimeError("missing predecessor edge on extracted cycle")
    edge_indices.reverse()  # predecessor order -> forward cycle order

    total = 0
    for i, edge_index in enumerate(edge_indices):
        src, dst, weight, _ci = system.edges[edge_index]
        nxt = system.edges[edge_indices[(i + 1) % len(edge_indices)]]
        if dst != nxt[0]:
            raise RuntimeError("extracted edges do not form a directed cycle")
        total += weight
    if total >= 0:
        raise RuntimeError("extracted cycle is not negative")
    if len({system.edges[e][0] for e in edge_indices}) != len(edge_indices):
        raise RuntimeError("extracted cycle repeats a vertex")

    constraint_indices = tuple(system.edges[e][3] for e in edge_indices)
    return constraint_indices, total


def find_negative_cycle(
    system: DifferenceSystem,
    blocked: Optional[Set[int]] = None,
) -> Optional[Tuple[Tuple[int, ...], int]]:
    """Return ``(constraint_indices_in_cycle_order, total_weight)`` or ``None``.

    Bellman-Ford from a virtual super-source connected to every vertex with
    a zero-weight edge, so every component is inspected.  ``blocked`` holds
    constraint indices temporarily removed from the graph (used by the MUS
    enumerator).  Runs in O(V * E) time and O(V) extra space.
    """
    blocked = blocked or frozenset()
    n = system.variable_count
    if n == 0:
        return None
    # Materialize the active edge list once; the hot loop below then avoids
    # per-iteration membership tests and tuple unpacking of constraint ids.
    active = [
        (edge_index, src, dst, weight)
        for edge_index, (src, dst, weight, cidx) in enumerate(system.edges)
        if cidx not in blocked
    ]
    dist = [0] * n
    pred_vertex = [-1] * n
    pred_edge = [-1] * n
    changed = -1
    for _ in range(n):
        changed = -1
        for edge_index, src, dst, weight in active:
            candidate = dist[src] + weight
            if candidate < dist[dst]:
                dist[dst] = candidate
                pred_vertex[dst] = src
                pred_edge[dst] = edge_index
                changed = dst
        if changed == -1:
            return None
    return _extract_cycle(system, pred_vertex, pred_edge, changed)


def is_satisfiable(system: DifferenceSystem, blocked: Optional[Set[int]] = None) -> bool:
    return find_negative_cycle(system, blocked) is None


def satisfying_assignment(system: DifferenceSystem) -> Optional[Dict[str, int]]:
    """Return a satisfying integer assignment, or ``None`` if unsatisfiable."""
    n = system.variable_count
    if n == 0:
        return {}
    dist = [0] * n
    for _ in range(n):
        changed = False
        for src, dst, weight, _cidx in system.edges:
            candidate = dist[src] + weight
            if candidate < dist[dst]:
                dist[dst] = candidate
                changed = True
        if not changed:
            return {name: dist[i] for i, name in enumerate(system.variables)}
    return None


def enumerate_minimal_conflicts(
    system: DifferenceSystem,
    max_conflicts: int = 10,
    oracle_budget: int = 1000,
) -> Tuple[List[Tuple[Tuple[int, ...], int]], bool, int]:
    """Enumerate distinct simple negative cycles (= minimal conflict sets).

    Method: find one negative cycle, then recursively retry with each of its
    edges individually blocked.  Every simple negative cycle is a MUS, and
    every MUS is a simple negative cycle, so this enumerates exactly the
    minimal conflict sets.  Terminates when (a) no negative cycle remains in
    every branch, (b) ``max_conflicts`` distinct sets were reported, or
    (c) ``oracle_budget`` oracle calls were spent.  Returns
    ``(conflicts, truncated, oracle_calls)``.
    """
    if max_conflicts < 1:
        raise ValueError("max_conflicts must be >= 1")
    if oracle_budget < 1:
        raise ValueError("oracle_budget must be >= 1")

    results: List[Tuple[Tuple[int, ...], int]] = []
    seen: Set[frozenset] = set()
    explored_blocks: Set[frozenset] = set()
    truncated = False
    calls = 0
    stack = [frozenset()]

    # Explicit DFS stack (rather than recursion) so constraint count does not
    # impose a recursion-depth limit.
    while stack:
        if len(results) >= max_conflicts or calls >= oracle_budget:
            truncated = True
            break
        blocked = stack.pop()
        if blocked in explored_blocks:
            continue
        explored_blocks.add(blocked)
        calls += 1
        found = find_negative_cycle(system, set(blocked))
        if found is None:
            continue
        indices, total = found
        key = frozenset(indices)
        if key not in seen:
            seen.add(key)
            results.append((indices, total))
            if len(results) >= max_conflicts:
                truncated = True
                break
        # reversed(): deterministic, lexicographically first edge explored first.
        for cidx in reversed(indices):
            child = blocked | {cidx}
            if child not in explored_blocks:
                stack.append(child)

    # Deterministic, most-actionable first: smaller conflicts first.
    results.sort(key=lambda item: (len(item[0]), item[0]))
    return results, truncated, calls
