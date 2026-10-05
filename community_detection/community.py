"""Deterministic community detection on undirected, optionally weighted graphs.

Pure Python standard library only. Implements the Louvain greedy modularity
maximisation algorithm (Blondel, Guillaume, Lambiotte & Lefebvre, 2008) with
fully deterministic tie breaking, explicit iteration caps, and deterministic
handling of isolated vertices and self loops.

Edge storage convention
-----------------------
* A normal undirected edge of weight w is stored symmetrically:
  adj[u][v] == adj[v][u] == w.
* A self loop of weight w is stored once: adj[u][u] == w.
* degree(u) = sum(adj[u].values()).  A normal edge therefore contributes 2w
  to the total degree; a self loop contributes 2w as well (it has two
  incidences at u).
* m2 = sum_u degree(u) = 2 * (total edge weight, self loops included).

Modularity (Newman convention, self loops included)
        Q = sum_C [ 2*sigma_in(C) / m2 - (sigma_tot(C) / m2)^2 ]
where sigma_tot(C) = sum_{u in C} degree(u), and sigma_in(C) is the sum of
weights of edges whose two ends lie in C (each edge counted once; a self
loop's weight is counted once).  With u temporarily removed from its current
community, inserting u (degree k_u, links c_in(u->C) into community C) into
community C changes Q by

        dQ_insert = (2 / m2) * (c_in(u->C) - sigma_tot(C) * k_u / m2).

A node is moved only into a community whose score strictly beats the score
of staying in its current community.
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

EPS = 1e-10
DEFAULT_MAX_PASSES = 100
DEFAULT_MAX_LEVELS = 64


class Graph:
    """Adjacency-map undirected graph; parallel edges merge by weight."""

    def __init__(self) -> None:
        self.adj: Dict[int, Dict[int, float]] = defaultdict(dict)
        self.nodes: set = set()

    def add_node(self, u: int) -> None:
        self.nodes.add(u)
        _ = self.adj[u]

    def add_edge(self, u: int, v: int, weight: float = 1.0) -> None:
        if weight <= 0:
            raise ValueError("edge weight must be positive")
        self.nodes.add(u)
        self.nodes.add(v)
        if u == v:
            self.adj[u][u] = self.adj[u].get(u, 0.0) + weight
        else:
            self.adj[u][v] = self.adj[u].get(v, 0.0) + weight
            self.adj[v][u] = self.adj[v].get(u, 0.0) + weight

    def degree(self, u: int) -> float:
        return sum(self.adj[u].values())

    def self_loop_weight(self, u: int) -> float:
        return self.adj[u].get(u, 0.0)


def graph_from_edges(
    edges: Iterable[Tuple[int, int]],
    nodes: Optional[Iterable[int]] = None,
    weights: Optional[Iterable[float]] = None,
) -> Graph:
    g = Graph()
    if nodes is not None:
        for n in nodes:
            g.add_node(n)
    if weights is None:
        for u, v in edges:
            g.add_edge(u, v)
    else:
        for (u, v), w in zip(edges, weights):
            g.add_edge(u, v, w)
    return g


def _index_graph(g: Graph) -> Tuple[List[Dict[int, float]], List[int]]:
    """Map arbitrary (hashable, orderable) node ids to dense 0..n-1 indices."""
    order = sorted(g.nodes)
    index = {u: i for i, u in enumerate(order)}
    n = len(order)
    adj: List[Dict[int, float]] = [dict() for _ in range(n)]
    for u in order:
        iu = index[u]
        for v, w in g.adj[u].items():
            adj[iu][index[v]] = w
    return adj, order


def _one_level(
    adj: List[Dict[int, float]],
    degree: List[float],
    m2: float,
    rng: random.Random,
    max_passes: int,
    shuffle: bool,
) -> Tuple[List[int], int, bool]:
    """Greedy local modularity optimisation on one Louvain level.

    Returns (community_of_node, passes_used, stopped_by_cap).
    """
    n = len(adj)
    community = list(range(n))
    # Size of a community in terms of total degree of its members.
    com_tot = list(degree)
    order = list(range(n))

    passes = 0
    stopped_by_cap = False
    while passes < max_passes:
        if shuffle:
            rng.shuffle(order)
        moves = 0
        for u in order:
            ku = degree[u]
            if ku == 0.0:
                # Isolated vertex: k=0 gives dQ=0 for every community.
                # Keep it deterministically in its own singleton community.
                continue
            current = community[u]

            # Total weight of links from u to each neighbouring community,
            # excluding the self-loop weight (its two ends stay with u).
            link: Dict[int, float] = defaultdict(float)
            for v, w in adj[u].items():
                if v == u:
                    continue
                link[community[v]] += w

            com_tot[current] -= ku

            # Score of staying put (u rejoins its current community).
            best_score = link.get(current, 0.0) - ku * com_tot[current] / m2
            best_com = current
            # Iterate candidate communities in ascending id order so an exact
            # tie keeps the smallest id (fully deterministic).
            for c in sorted(link):
                score = link[c] - ku * com_tot[c] / m2
                if score > best_score + EPS:
                    best_score = score
                    best_com = c

            community[u] = best_com
            com_tot[best_com] += ku
            if best_com != current:
                moves += 1
        passes += 1
        if moves == 0:
            break
    else:
        stopped_by_cap = True
    return community, passes, stopped_by_cap


def _aggregate(
    adj: List[Dict[int, float]],
    degree: List[float],
    community: List[int],
) -> Tuple[List[Dict[int, float]], List[float]]:
    """Build the weighted meta-graph induced by the partition."""
    # Compact community ids to 0..k-1 preserving first-appearance order.
    remap: Dict[int, int] = {}
    for c in community:
        if c not in remap:
            remap[c] = len(remap)
    k = len(remap)
    meta: List[Dict[int, float]] = [dict() for _ in range(k)]
    meta_deg = [0.0] * k

    for u, nbrs in enumerate(adj):
        cu = remap[community[u]]
        for v, w in nbrs.items():
            cv = remap[community[v]]
            if cu == cv:
                # Internal edge (or self loop): store once at meta[cu][cu],
                # contributing 2w to the meta-node's degree.
                meta[cu][cu] = meta[cu].get(cu, 0.0) + 2.0 * w
                meta_deg[cu] += 2.0 * w
            else:
                if u < v:  # count each cross edge once while adding both ways
                    meta[cu][cv] = meta[cu].get(cv, 0.0) + w
                    meta[cv][cu] = meta[cv].get(cu, 0.0) + w
                    meta_deg[cu] += w
                    meta_deg[cv] += w
    return meta, meta_deg


def _renumber(partition: Dict[int, int]) -> Dict[int, int]:
    """Canonical community ids: ordered by smallest original node in each."""
    members: Dict[int, List[int]] = defaultdict(list)
    for node, c in partition.items():
        members[c].append(node)
    canonical: Dict[int, int] = {}
    for new_id, c in enumerate(sorted(members, key=lambda c: min(members[c]))):
        for node in members[c]:
            canonical[node] = new_id
    return canonical


def detect_communities(
    g: Graph,
    seed: int = 42,
    max_passes: int = DEFAULT_MAX_PASSES,
    max_levels: int = DEFAULT_MAX_LEVELS,
    order: str = "sorted",
) -> Dict[str, object]:
    """Run deterministic Louvain.

    `order` controls the node visit order inside the local phase:
    "sorted"   - ascending node id every pass; the seed is then irrelevant
                 to the result (strongest reproducibility guarantee);
    "shuffled" - seeded random permutation per pass (classic Louvain);
                 identical seeds give identical partitions.

    Returns a dict with:
      partition      : {node: canonical_community_id}
      communities    : number of communities
      modularity     : modularity of the final partition
      levels         : number of aggregation levels used
      passes_per_level: local passes consumed at each level
      pass_cap_levels: levels whose local phase hit the pass cap (the
                       algorithm still aggregated and continued; the cap
                       only bounds work per level)
      stop_reason    : why the outer loop terminated
      seed           : the seed used
    """
    if order not in ("sorted", "shuffled"):
        raise ValueError("order must be 'sorted' or 'shuffled'")
    shuffle = order == "shuffled"
    adj, order = _index_graph(g)
    n = len(adj)
    degree = [sum(nbrs.values()) for nbrs in adj]
    m2 = sum(degree)

    # No edges at all: every node is deterministically its own community.
    if m2 == 0.0:
        partition = {u: i for i, u in enumerate(order)}
        return {
            "partition": partition,
            "communities": n,
            "modularity": 0.0,
            "levels": 0,
            "passes_per_level": [],
            "stop_reason": "edgeless_graph",
            "seed": seed,
            "order": order,
            "pass_cap_levels": [],
        }

    # Mapping from the *current-level* node index back to original node ids.
    super_of: List[List[int]] = [[u] for u in range(n)]
    partition_orig: Dict[int, int] = {i: i for i in range(n)}
    levels = 0
    passes_per_level: List[int] = []
    pass_cap_levels: List[int] = []
    stop_reason = "no_merge_level"

    while levels < max_levels:
        rng = random.Random(seed + levels)
        community, passes, capped = _one_level(
            adj, degree, m2, rng, max_passes, shuffle
        )
        passes_per_level.append(passes)
        if capped:
            pass_cap_levels.append(levels)
        levels += 1

        num_com = len(set(community))
        if num_com == len(adj):
            stop_reason = "no_merge_level"
            break

        # Accumulate into the original-node partition.
        new_super: List[List[int]] = [[] for _ in range(num_com)]
        next_partition: Dict[int, int] = {}
        # Remap community labels to compact ids.
        remap: Dict[int, int] = {}
        for c in community:
            if c not in remap:
                remap[c] = len(remap)
        for cur_idx, orig_nodes in enumerate(super_of):
            cid = remap[community[cur_idx]]
            for orig in orig_nodes:
                next_partition[orig] = cid
            new_super[cid].extend(orig_nodes)

        meta, meta_deg = _aggregate(adj, degree, community)

        adj, degree, super_of = meta, meta_deg, new_super
        partition_orig = next_partition

        if num_com == 1:
            stop_reason = "single_community"
            break
    else:
        stop_reason = "max_levels_cap_reached"

    # Translate dense original indices back to original node ids.
    raw = {order[idx]: cid for idx, cid in partition_orig.items()}
    partition = _renumber(raw)
    q = modularity(g, partition)
    return {
        "partition": partition,
        "communities": len(set(partition.values())),
        "modularity": q,
        "levels": levels,
        "passes_per_level": passes_per_level,
        "pass_cap_levels": pass_cap_levels,
        "stop_reason": stop_reason,
        "seed": seed,
        "order": order,
    }


def modularity(g: Graph, partition: Dict[int, int]) -> float:
    """Newman modularity of `partition` on `g` (self loops included)."""
    sigma_tot: Dict[int, float] = defaultdict(float)
    for u in g.nodes:
        sigma_tot[partition[u]] += g.degree(u)
    m2 = sum(sigma_tot.values())
    if m2 == 0.0:
        return 0.0

    sigma_in: Dict[int, float] = defaultdict(float)
    seen_edges: set = set()
    for u in g.nodes:
        cu = partition[u]
        for v, w in g.adj[u].items():
            if partition[v] != cu:
                continue
            if u == v:
                sigma_in[cu] += w  # self loop counted once
            elif (v, u) not in seen_edges:
                seen_edges.add((u, v))
                sigma_in[cu] += w
    return sum(
        2.0 * sigma_in[c] / m2 - (sigma_tot[c] / m2) ** 2
        for c in sigma_tot
    )
