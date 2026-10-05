"""Partition quality metrics and per-community comparison tables.

All metrics are computed on the ORIGINAL graph (not the aggregated
meta-graph) and follow the edge conventions of community.py:

* normal edge weight w contributes w to cut/internal weight sums and 2w to
  the total degree;
* self loop weight w contributes w to internal weight and 2w to the degree
  of its endpoint;
* m2 = sum of all degrees = 2 * total edge weight.

Definitions for a community C (n_C = |C|):
  internal_weight I(C) : weight of edges with both ends in C (self loops
                         counted once)
  cut_weight    X(C)   : weight of edges with exactly one end in C
  volume        T(C)   : sum of degrees of nodes in C = 2*I(C) + X(C)
  conductance   phi(C) : X(C) / min(T(C), m2 - T(C))   (0 if denominator 0)
  density       d(C)   : I(C) / (n_C*(n_C-1)/2 + s_C) where s_C is the
                         number of distinct self loops inside C
                         (unweighted convention: 1.0 means "every possible
                         position is filled"); 0.0 for singletons without
                         self loops, 1.0 for a singleton with a self loop.

Global metrics:
  modularity Q  : Newman modularity (see community.modularity)
  coverage      : fraction of total edge weight whose ends share a community
                  = sum_C I(C) / (m2/2)
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List

from community import Graph, modularity


def _community_stats(g: Graph, partition: Dict[int, int]):
    members: Dict[int, List[int]] = defaultdict(list)
    for node in g.nodes:
        members[partition[node]].append(node)

    volume: Dict[int, float] = defaultdict(float)
    for node in g.nodes:
        volume[partition[node]] += g.degree(node)

    internal: Dict[int, float] = defaultdict(float)
    cut: Dict[int, float] = defaultdict(float)
    self_loops: Dict[int, int] = defaultdict(int)
    seen = set()
    for u in g.nodes:
        cu = partition[u]
        for v, w in g.adj[u].items():
            cv = partition[v]
            if u == v:
                internal[cu] += w
                self_loops[cu] += 1
            elif cu == cv:
                if (v, u) not in seen:
                    seen.add((u, v))
                    internal[cu] += w
            else:
                cut[cu] += w  # each crossing edge counted once per endpoint
    m2 = sum(volume.values())
    return members, volume, internal, cut, self_loops, m2


def community_report(g: Graph, partition: Dict[int, int]) -> List[dict]:
    """Per-community comparison table (sorted by community id)."""
    members, volume, internal, cut, self_loops, m2 = _community_stats(
        g, partition
    )
    rows = []
    for c in sorted(members):
        n_c = len(members[c])
        vol = volume[c]
        denom = min(vol, m2 - vol)
        conductance = (cut[c] / denom) if denom > 0 else 0.0
        slots = n_c * (n_c - 1) / 2 + self_loops[c]
        density = (internal[c] / slots) if slots > 0 else 0.0
        rows.append(
            {
                "community": c,
                "size": n_c,
                "internal_weight": internal[c],
                "cut_weight": cut[c],
                "volume": vol,
                "self_loops": self_loops[c],
                "conductance": conductance,
                "density": density,
            }
        )
    return rows


def global_metrics(g: Graph, partition: Dict[int, int]) -> dict:
    members, volume, internal, cut, self_loops, m2 = _community_stats(
        g, partition
    )
    total_weight = m2 / 2.0
    coverage = (
        sum(internal.values()) / total_weight if total_weight > 0 else 1.0
    )
    return {
        "nodes": len(g.nodes),
        "edges_total_weight": total_weight,
        "communities": len(members),
        "modularity": modularity(g, partition),
        "coverage": coverage,
        "mean_conductance": (
            sum(
                (cut[c] / min(volume[c], m2 - volume[c]))
                if min(volume[c], m2 - volume[c]) > 0
                else 0.0
                for c in members
            )
            / len(members)
            if members
            else 0.0
        ),
    }


def pairwise_cut_matrix(g: Graph, partition: Dict[int, int]) -> Dict[tuple, float]:
    """Edge weight crossing each ordered pair of distinct communities."""
    matrix: Dict[tuple, float] = defaultdict(float)
    for u in g.nodes:
        cu = partition[u]
        for v, w in g.adj[u].items():
            cv = partition[v]
            if u != v and cu != cv:
                matrix[(cu, cv)] += w  # counted once from each side
    return dict(matrix)


def adjusted_rand_index(a: Dict[int, int], b: Dict[int, int]) -> float:
    """ARI between two labelings on the same node set (1.0 == identical)."""
    assert set(a) == set(b), "labelings must cover the same node set"
    n = len(a)
    if n < 2:
        return 1.0
    table: Dict[tuple, int] = defaultdict(int)
    row_sum: Dict[int, int] = defaultdict(int)
    col_sum: Dict[int, int] = defaultdict(int)
    for node in a:
        table[(a[node], b[node])] += 1
        row_sum[a[node]] += 1
        col_sum[b[node]] += 1

    def comb2(x: int) -> float:
        return x * (x - 1) / 2.0

    index = sum(comb2(c) for c in table.values())
    sum_rows = sum(comb2(c) for c in row_sum.values())
    sum_cols = sum(comb2(c) for c in col_sum.values())
    total = comb2(n)
    if total == 0:
        return 1.0
    expected = sum_rows * sum_cols / total
    maximum = (sum_rows + sum_cols) / 2.0
    if maximum == expected:
        return 1.0
    return (index - expected) / (maximum - expected)


def format_report(g: Graph, partition: Dict[int, int]) -> str:
    """Human-readable metrics report with the community comparison table."""
    gm = global_metrics(g, partition)
    rows = community_report(g, partition)
    lines = [
        "global: nodes={nodes} total_edge_weight={edges_total_weight} "
        "communities={communities} modularity={modularity:.6f} "
        "coverage={coverage:.6f} mean_conductance={mean_conductance:.6f}".format(
            **gm
        ),
        "community | size | internal_w | cut_w | volume | self_loops | "
        "conductance | density",
    ]
    for r in rows:
        lines.append(
            "{community:>9} | {size:>4} | {internal_weight:>10.1f} | "
            "{cut_weight:>5.1f} | {volume:>6.1f} | {self_loops:>10} | "
            "{conductance:>11.4f} | {density:.4f}".format(**r)
        )
    cross = pairwise_cut_matrix(g, partition)
    if cross:
        lines.append("cross-community edge weight (directed counts):")
        for (a, b), w in sorted(cross.items()):
            lines.append("  {} -> {} : {:.1f}".format(a, b, w))
    return "\n".join(lines)
