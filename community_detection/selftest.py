#!/usr/bin/env python3
"""Self tests + reproducibility cross-check ("duipai") data.

Run:  python3 selftest.py
Writes:
  out/selftest_data.json     machine-readable cross-run data
  out/selftest_report.txt    metrics tables for the small cases
Exits non-zero if any check fails.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import graphs
import metrics
from community import Graph, detect_communities

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def canonical_ok(partition: dict) -> bool:
    labels = sorted(set(partition.values()))
    return labels == list(range(len(labels)))


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    report_lines = []
    data = {"cases": []}

    def run_case(name, g, seeds=(42, 42, 43, 2024)):
        results = [detect_communities(g, seed=s) for s in seeds]
        partitions = [r["partition"] for r in results]
        identical = partitions[0] == partitions[1]
        ari_same = metrics.adjusted_rand_index(partitions[0], partitions[1])
        ari_seed43 = metrics.adjusted_rand_index(partitions[0], partitions[2])
        ari_seed2024 = metrics.adjusted_rand_index(partitions[0], partitions[3])
        q_values = [r["modularity"] for r in results]
        check(f"{name}: canonical labels 0..k-1", canonical_ok(partitions[0]))
        check(
            f"{name}: same seed -> identical partitions",
            identical and ari_same == 1.0,
            f"ARI(seed42,seed42')={ari_same:.6f}",
        )
        # sorted visit order: result must not depend on the seed at all
        check(
            f"{name}: sorted order -> seed-independent partitions",
            ari_seed43 == 1.0 and ari_seed2024 == 1.0,
            f"ARI vs seed43={ari_seed43:.6f}, vs seed2024={ari_seed2024:.6f}",
        )
        # shuffled visit order: same seed must still reproduce exactly
        sh_a = detect_communities(g, seed=42, order="shuffled")["partition"]
        sh_b = detect_communities(g, seed=42, order="shuffled")["partition"]
        check(f"{name}: shuffled order, same seed -> identical",
              sh_a == sh_b)
        print(
            f"        cross-seed stability: ARI vs seed43={ari_seed43:.6f}, "
            f"vs seed2024={ari_seed2024:.6f}; "
            f"Q range [{min(q_values):.6f},{max(q_values):.6f}]"
        )
        report_lines.append("=" * 78)
        report_lines.append(f"CASE {name}  (seed=42)")
        report_lines.append(metrics.format_report(g, partitions[0]))
        data["cases"].append(
            {
                "case": name,
                "nodes": len(g.nodes),
                "edge_weight": sum(g.degree(u) for u in g.nodes) / 2,
                "seeds": list(seeds),
                "identical_same_seed": identical,
                "ari_same_seed": ari_same,
                "ari_seed42_vs_seed43": ari_seed43,
                "ari_seed42_vs_seed2024": ari_seed2024,
                "modularity_per_run": q_values,
                "communities_per_run": [r["communities"] for r in results],
                "levels": results[0]["levels"],
                "passes_per_level": results[0]["passes_per_level"],
                "stop_reason": results[0]["stop_reason"],
                "partition_seed42": {str(k): v for k, v in partitions[0].items()},
            }
        )
        return results[0]

    # 1) chain graph -------------------------------------------------------
    chain = graphs.chain_graph(40)
    r = run_case("chain_40", chain)
    p = r["partition"]
    contiguous = all(
        abs(sorted(u for u in chain.nodes if p[u] == c)[-1]
            - sorted(u for u in chain.nodes if p[u] == c)[0])
        + 1 == sum(1 for u in chain.nodes if p[u] == c)
        for c in set(p.values())
    )
    check("chain: every community is a contiguous segment", contiguous)
    check("chain: modularity > 0.5", r["modularity"] > 0.5,
          f"Q={r['modularity']:.4f}")

    # 2) two clearly separated cliques ------------------------------------
    cliques = graphs.two_cliques(30)
    r = run_case("two_cliques_2x30_bridge", cliques)
    p = r["partition"]
    recovered = (
        r["communities"] == 2
        and len({p[u] for u in range(30)}) == 1
        and len({p[u] for u in range(30, 60)}) == 1
        and p[0] != p[30]
    )
    check("two cliques: exactly the two cliques recovered", recovered,
          f"communities={r['communities']} Q={r['modularity']:.4f}")
    check("two cliques: modularity > 0.3", r["modularity"] > 0.3,
          f"Q={r['modularity']:.4f}")

    # 3) dense random graph ------------------------------------------------
    dense = graphs.dense_random(200, p=0.5, seed=7)
    r = run_case("dense_random_gnp_200_p0.5", dense)
    check("dense random: weak modularity (< 0.15) as expected",
          r["modularity"] < 0.15, f"Q={r['modularity']:.4f}")

    # 4) isolated vertices -------------------------------------------------
    iso = graphs.isolated_nodes_graph(n_edges_chain=10, n_isolated=5)
    r = run_case("chain_plus_5_isolated", iso)
    p = r["partition"]
    isolated = list(range(10, 15))
    singletons_ok = all(
        sum(1 for u in iso.nodes if p[u] == p[v]) == 1 for v in isolated
    )
    check("isolated vertices: each is a singleton community", singletons_ok)
    # deterministic ownership: each isolated node stays alone under any seed
    seed_labels = {
        s: detect_communities(iso, seed=s)["partition"] for s in (1, 100, 999)
    }
    stable = all(
        all(
            sum(1 for u in iso.nodes if seed_labels[s][u] == seed_labels[s][v])
            == 1
            for v in isolated
        )
        for s in seed_labels
    )
    check("isolated vertices: singleton under every seed", stable)

    # 5) self loops --------------------------------------------------------
    loop = graphs.self_loop_graph()
    r = run_case("self_loops", loop)
    p = r["partition"]
    check("pure self-loop node 3 is a singleton",
          sum(1 for u in loop.nodes if p[u] == p[3]) == 1)
    check("isolated node 5 is a singleton",
          sum(1 for u in loop.nodes if p[u] == p[5]) == 1)
    check("node 4 (loop+edge) joins the triangle side",
          p[4] == p[0])

    # 6) weighted ----------------------------------------------------------
    w = graphs.weighted_graph()
    r = run_case("weighted_two_cliques_weak_bridge", w)
    p = r["partition"]
    check("weighted: {0,1,2} vs {3,4,5} split by weight",
          {p[0], p[1], p[2]} != {p[3], p[4], p[5]}
          and p[0] == p[1] == p[2] and p[3] == p[4] == p[5])

    # 7) boundary: empty graph, single node, single edge -------------------
    empty = Graph()
    r0 = detect_communities(empty, seed=42)
    check("empty graph: 0 communities, stop=edgeless_graph",
          r0["communities"] == 0 and r0["stop_reason"] == "edgeless_graph",
          r0["stop_reason"])
    single = Graph()
    single.add_node(0)
    r1 = detect_communities(single, seed=42)
    check("single isolated node: one singleton community",
          r1["partition"] == {0: 0} and r1["communities"] == 1)
    edge = Graph()
    edge.add_edge(0, 1)
    r2 = detect_communities(edge, seed=42)
    check("single edge merges into one community, Q=0",
          r2["communities"] == 1 and abs(r2["modularity"]) < 1e-12,
          f"Q={r2['modularity']}")

    with open(os.path.join(OUT_DIR, "selftest_data.json"), "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    with open(os.path.join(OUT_DIR, "selftest_report.txt"), "w") as f:
        f.write("\n".join(report_lines) + "\n")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for msg in FAILURES:
            print("  -", msg)
        return 1
    print("All self-test checks passed.")
    print(f"Data written to {OUT_DIR}/selftest_data.json and selftest_report.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
