"""Deterministic test/benchmark graph generators (seeded, stdlib only)."""

from __future__ import annotations

import random
from typing import List, Tuple

from community import Graph


def chain_graph(n: int) -> Graph:
    """Path graph 0-1-...-(n-1)."""
    g = Graph()
    g.add_node(0)
    for i in range(n - 1):
        g.add_edge(i, i + 1)
    return g


def two_cliques(n_each: int = 30) -> Graph:
    """Two cliques of n_each nodes joined by a single bridge edge."""
    g = Graph()
    for base in (0, n_each):
        for i in range(n_each):
            for j in range(i + 1, n_each):
                g.add_edge(base + i, base + j)
    g.add_edge(n_each - 1, n_each)  # the bridge
    return g


def dense_random(n: int = 200, p: float = 0.5, seed: int = 7) -> Graph:
    """Dense Erdos-Renyi graph: essentially no community structure."""
    rng = random.Random(seed)
    g = Graph()
    for i in range(n):
        g.add_node(i)
    for i in range(n):
        for j in range(i + 1, n):
            if rng.random() < p:
                g.add_edge(i, j)
    return g


def isolated_nodes_graph(n_edges_chain: int = 10, n_isolated: int = 5) -> Graph:
    """A short chain plus n_isolated isolated vertices."""
    g = chain_graph(n_edges_chain)
    base = n_edges_chain
    for k in range(n_isolated):
        g.add_node(base + k)
    return g


def self_loop_graph() -> Graph:
    """Triangle + a pure-loop node + a loop node wired into the triangle."""
    g = Graph()
    for e in [(0, 1), (1, 2), (0, 2)]:
        g.add_edge(*e)
    g.add_edge(3, 3)          # node 3: only a self loop
    g.add_edge(4, 4)          # node 4: self loop plus links to all 3 nodes
    g.add_edge(4, 0)
    g.add_edge(4, 1)
    g.add_edge(4, 2)
    g.add_node(5)             # node 5: isolated
    return g


def weighted_graph() -> Graph:
    """Small weighted graph: heavy triangle vs light bridge."""
    g = Graph()
    g.add_edge(0, 1, 5.0)
    g.add_edge(1, 2, 5.0)
    g.add_edge(0, 2, 5.0)
    g.add_edge(2, 3, 0.1)
    g.add_edge(3, 4, 5.0)
    g.add_edge(4, 5, 5.0)
    g.add_edge(3, 5, 5.0)
    return g


def planted_benchmark(
    n: int = 100_000,
    m: int = 500_000,
    n_clusters: int = 40,
    intra_fraction: float = 0.9,
    n_isolated: int = 100,
    n_selfloops: int = 50,
    seed: int = 20261005,
) -> Tuple[Graph, dict]:
    """Planted-partition benchmark graph.

    n nodes, about m undirected edges, n_clusters equal planted clusters,
    plus n_isolated isolated vertices and n_selfloops self loops.

    The clusters are planted as triangle-rich Erdos-Renyi blocks: most
    intra-cluster edges are added in random triangles (real networks have
    high local clustering; a purely tree-like random graph is a known
    pathological local optimum for single-node greedy Louvain).  The
    remaining intra edges and all inter edges are random.  Determined by
    `seed` and fully deterministic.
    """
    rng = random.Random(seed)
    n_clustered = n - n_isolated
    cluster_of = [i % n_clusters for i in range(n_clustered)]
    rng.shuffle(cluster_of)
    members: List[List[int]] = [[] for _ in range(n_clusters)]
    for node, c in enumerate(cluster_of):
        members[c].append(node)

    g = Graph()
    for i in range(n):
        g.add_node(i)

    m_intra = int(m * intra_fraction)
    # 70% of the intra budget comes from triangles (each draw contributes
    # ~3 distinct edges), the rest from random intra edges.
    tri_draws = int(m_intra * 0.7) // 3
    extra_intra = m_intra - tri_draws * 3

    # Triangles, one cluster at a time (uniformly chosen per triangle).
    for _ in range(tri_draws):
        c = rng.randrange(n_clusters)
        mem = members[c]
        a, b, d = rng.sample(mem, 3)
        g.add_edge(a, b)
        g.add_edge(b, d)
        g.add_edge(a, d)
    # Extra random intra-cluster edges.
    for _ in range(extra_intra):
        c = rng.randrange(n_clusters)
        mem = members[c]
        u = mem[rng.randrange(len(mem))]
        v = mem[rng.randrange(len(mem))]
        if u != v:
            g.add_edge(u, v)
    # Inter-cluster random edges.
    added = 0
    while added < m - m_intra:
        u = rng.randrange(n_clustered)
        v = rng.randrange(n_clustered)
        if u != v and cluster_of[u] != cluster_of[v] and v not in g.adj[u]:
            g.add_edge(u, v)
            added += 1
    # Self loops on the first n_selfloops clustered nodes.
    for i in range(n_selfloops):
        g.add_edge(i, i)

    meta = {
        "n": n,
        "m_target": m,
        "n_clusters": n_clusters,
        "n_isolated": n_isolated,
        "n_selfloops": n_selfloops,
        "seed": seed,
        "planted_cluster_of": cluster_of,
    }
    return g, meta
