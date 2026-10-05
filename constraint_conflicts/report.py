"""Public conflict-analysis report and minimality verification helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .difference_logic import (
    DifferenceConstraint,
    DifferenceSystem,
    enumerate_minimal_conflicts,
    satisfying_assignment,
)


@dataclass(frozen=True)
class ConflictSet:
    """An inclusion-minimal unsatisfiable subset of constraints."""

    constraint_indices: Tuple[int, ...]
    constraint_ids: Tuple[str, ...]
    total_bound: int
    kind: str

    @property
    def size(self) -> int:
        return len(self.constraint_ids)

    @property
    def is_self_contradiction(self) -> bool:
        return self.kind == "self_contradiction"


@dataclass(frozen=True)
class RemovalCheck:
    """Result of testing MUS \\ {removed_constraint_id}."""

    removed_constraint_id: str
    remaining_constraints_acyclic: bool
    remaining_is_satisfiable: bool
    witness: Optional[Dict[str, int]]


@dataclass(frozen=True)
class MinimalityVerification:
    """Independent verification data for one reported conflict set."""

    constraint_ids: Tuple[str, ...]
    size: int
    total_bound: int
    is_negative_cycle: bool
    is_simple_directed_cycle: bool
    every_single_removal_is_satisfiable: bool
    removal_checks: Tuple[RemovalCheck, ...]

    @property
    def is_minimal(self) -> bool:
        return (
            self.is_negative_cycle
            and self.is_simple_directed_cycle
            and self.every_single_removal_is_satisfiable
        )


@dataclass(frozen=True)
class AnalysisReport:
    """Result of analyzing a complete constraint set."""

    status: str
    constraint_count: int
    variable_count: int
    conflicts: Tuple[ConflictSet, ...] = ()
    witness: Optional[Dict[str, int]] = None
    oracle_calls: int = 0
    max_conflicts: int = 10
    oracle_budget: int = 1000
    truncated: bool = False
    self_contradictory_ids: Tuple[str, ...] = ()

    @property
    def has_conflict(self) -> bool:
        return self.status == "unsatisfiable"

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def analyze(
    constraints: Iterable[DifferenceConstraint],
    max_conflicts: int = 10,
    oracle_budget: int = 1000,
) -> AnalysisReport:
    """Analyze constraints and report no conflict or up to ``max_conflicts`` MUSs.

    ``status == "no_conflict"`` comes with a witness assignment and no
    conflicts.  ``status == "unsatisfiable"`` comes with one or more proven
    minimal conflict sets; size-one conflicts are also marked as
    self-contradictory constraints.
    """
    system = constraints if isinstance(constraints, DifferenceSystem) else DifferenceSystem(constraints)
    found, truncated, oracle_calls = enumerate_minimal_conflicts(
        system,
        max_conflicts=max_conflicts,
        oracle_budget=oracle_budget,
    )

    if not found:
        return AnalysisReport(
            status="no_conflict",
            constraint_count=system.constraint_count,
            variable_count=system.variable_count,
            witness=satisfying_assignment(system),
            oracle_calls=oracle_calls,
            max_conflicts=max_conflicts,
            oracle_budget=oracle_budget,
            truncated=truncated,
        )

    conflicts: List[ConflictSet] = []
    self_contradictory: List[str] = []
    for indices, total in found:
        ids = tuple(system.constraints[i].id for i in indices)
        kind = "cycle"
        if len(indices) == 1:
            c = system.constraints[indices[0]]
            if c.lower == c.upper and c.bound < 0:
                kind = "self_contradiction"
                self_contradictory.append(c.id)
        conflicts.append(
            ConflictSet(
                constraint_indices=indices,
                constraint_ids=ids,
                total_bound=total,
                kind=kind,
            )
        )

    return AnalysisReport(
        status="unsatisfiable",
        constraint_count=system.constraint_count,
        variable_count=system.variable_count,
        conflicts=tuple(conflicts),
        witness=None,
        oracle_calls=oracle_calls,
        max_conflicts=max_conflicts,
        oracle_budget=oracle_budget,
        truncated=truncated,
        self_contradictory_ids=tuple(self_contradictory),
    )


def _acyclic_witness(system: DifferenceSystem, indices: Sequence[int]) -> Dict[str, int]:
    n = system.variable_count
    chosen = set(indices)
    indeg = [0] * n
    touched = set()
    for edge_index, (src, dst, _w, cidx) in enumerate(system.edges):
        if cidx in chosen:
            touched.add(src)
            touched.add(dst)
            indeg[dst] += 1

    queue = [v for v in touched if indeg[v] == 0]
    distance = {v: 0 for v in touched}
    processed = 0
    while queue:
        src = queue.pop()
        processed += 1
        for edge_index in system.adjacency[src]:
            _u, dst, weight, cidx = system.edges[edge_index]
            if cidx not in chosen:
                continue
            candidate = distance[src] + weight
            if candidate < distance[dst]:
                distance[dst] = candidate
            indeg[dst] -= 1
            if indeg[dst] == 0:
                queue.append(dst)

    if processed != len(touched):
        raise ValueError("subset is cyclic, so no topological witness exists")
    return {system.variables[v]: distance.get(v, 0) for v in touched}


def _is_simple_directed_cycle(
    system: DifferenceSystem,
    indices: Sequence[int],
) -> Tuple[bool, int]:
    if not indices:
        return False, 0
    chosen = set(indices)
    if len(chosen) != len(indices):
        return False, 0

    n = system.variable_count
    indeg = [0] * n
    outdeg = [0] * n
    total = 0
    first_src = -1
    for cidx in indices:
        src, dst, weight, _ci = system.edges[cidx]
        outdeg[src] += 1
        indeg[dst] += 1
        total += weight
        first_src = src if first_src < 0 else first_src

    vertices = set()
    for cidx in indices:
        src, dst, _w, _ci = system.edges[cidx]
        vertices.add(src)
        vertices.add(dst)
    if len(vertices) != len(indices):
        return False, total
    if any(indeg[v] != 1 or outdeg[v] != 1 for v in vertices):
        return False, total

    visited = 0
    current = first_src
    seen = set()
    for _ in range(len(indices)):
        if current in seen:
            return False, total
        seen.add(current)
        visited += 1
        next_vertices = []
        for edge_index in system.adjacency[current]:
            _u, dst, _w, cidx = system.edges[edge_index]
            if cidx in chosen:
                next_vertices.append(dst)
        if len(next_vertices) != 1:
            return False, total
        current = next_vertices[0]
    return current == first_src and visited == len(indices), total


def verify_assignment(
    system: DifferenceSystem,
    assignment: Dict[str, int],
) -> bool:
    for c in system.constraints:
        if assignment[c.lower] > assignment[c.upper] + c.bound:
            return False
    return True


def verify_minimality(
    system: DifferenceSystem,
    conflict: ConflictSet,
) -> MinimalityVerification:
    """Independently verify that every one-edge deletion is satisfiable."""
    indices = list(conflict.constraint_indices)
    simple, total = _is_simple_directed_cycle(system, indices)
    checks: List[RemovalCheck] = []

    for removed in indices:
        remaining = tuple(i for i in indices if i != removed)
        try:
            witness = _acyclic_witness(system, remaining)
            acyclic = True
            satisfiable = True
        except ValueError:
            witness = None
            acyclic = False
            satisfiable = False
        checks.append(
            RemovalCheck(
                removed_constraint_id=system.constraints[removed].id,
                remaining_constraints_acyclic=acyclic,
                remaining_is_satisfiable=satisfiable,
                witness=witness,
            )
        )

    return MinimalityVerification(
        constraint_ids=conflict.constraint_ids,
        size=len(indices),
        total_bound=total,
        is_negative_cycle=total < 0,
        is_simple_directed_cycle=simple,
        every_single_removal_is_satisfiable=all(
            c.remaining_is_satisfiable for c in checks
        ),
        removal_checks=tuple(checks),
    )
