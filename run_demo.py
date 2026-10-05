"""Demo + benchmark driver for redirect_trace.

Produces artifacts under ./out/:
  chain_records.json  - full hop-by-hop records for the demo scenarios
  policy_matrix.md    - header-stripping policy comparison table
  perf.json           - 10k-chain benchmark: timing + depth stats

Run: python3 run_demo.py
"""

import json
import random
import statistics
import time
from pathlib import Path

from redirect_trace import (
    RedirectTracer,
    ScriptedFetcher,
    apply_header_policy,
    normalize_url,
)

OUT = Path(__file__).parent / "out"
AUTH = {"Authorization": "Bearer s3cret", "Cookie": "sid=abc", "Accept": "*/*"}

SCENARIOS = {
    "direct_redirect_same_origin": (
        "https://shop.example/",
        {"https://shop.example/": (302, "https://shop.example/home"),
         "https://shop.example/home": (200, None)},
    ),
    "cross_protocol_downgrade": (
        "https://pay.example/start",
        {"https://pay.example/start": (301, "http://pay.example/landing"),
         "http://pay.example/landing": (200, None)},
    ),
    "cross_host_redirect": (
        "https://a.example/go",
        {"https://a.example/go": (302, "https://cdn.b.example/asset"),
         "https://cdn.b.example/asset": (200, None)},
    ),
    "redirect_loop": (
        "https://loop.example/a",
        {"https://loop.example/a": (302, "/b"),
         "https://loop.example/b": (302, "a")},
    ),
    "overlong_chain": (
        "https://deep.example/0",
        {f"https://deep.example/{i}": (302, f"https://deep.example/{i + 1}")
         for i in range(50)},
    ),
    "relative_and_escaped": (
        "https://web.example/dir/page",
        {"https://web.example/dir/page": (302, "../%7Euser/./x"),
         "https://web.example/~user/x": (302, "/final"),
         "https://web.example/final": (200, None)},
    ),
    "invalid_target_scheme": (
        "https://evil.example/",
        {"https://evil.example/": (302, "javascript:alert(1)")},
    ),
    "redirect_without_location": (
        "https://broken.example/",
        {"https://broken.example/": (302, None)},
    ),
    "url_credentials_start": (
        "https://user:pw@auth.example/login",
        {"https://auth.example/login": (302, "https://other.example/"),
         "https://other.example/": (200, None)},
    ),
}


def run_scenarios():
    records = {}
    for name, (start, script) in SCENARIOS.items():
        fetcher = ScriptedFetcher(script)
        result = RedirectTracer(fetcher, max_hops=10).trace(start, AUTH)
        records[name] = result.to_dict()
    return records


def policy_matrix():
    base = normalize_url("https://a.example:443/app")
    cases = [
        ("same origin (identical URL)", "https://a.example/app"),
        ("same origin, default port explicit", "https://a.example:443/app"),
        ("same origin, different path", "https://a.example/other"),
        ("cross host", "https://b.example/app"),
        ("cross subdomain", "https://cdn.a.example/app"),
        ("cross scheme downgrade https->http", "http://a.example/app"),
        ("cross scheme upgrade http->https", None),  # handled below
        ("cross port", "https://a.example:8443/app"),
    ]
    rows = []
    for label, target in cases:
        if target is None:
            prev = normalize_url("http://a.example/app")
            nxt = normalize_url("https://a.example/app")
        else:
            prev, nxt = base, normalize_url(target)
        _, stripped = apply_header_policy(prev, nxt, AUTH)
        kept = sorted(k for k in AUTH if k not in stripped)
        rows.append({
            "case": label,
            "from": prev.url,
            "to": nxt.url,
            "kept": kept,
            "stripped": stripped,
        })
    return rows


def write_policy_md(rows):
    lines = [
        "# Sensitive-Header Policy Matrix",
        "",
        "Policy: `Authorization`, `Proxy-Authorization`, `Cookie` are forwarded",
        "only within the same origin `(scheme, host, effective port)`. Any",
        "cross-scheme or cross-host hop strips them; stripped headers are",
        "never re-added later in the chain.",
        "",
        "| Case | From | To | Kept | Stripped |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            "| {case} | `{frm}` | `{to}` | {kept} | {stripped} |".format(
                case=r["case"], frm=r["from"], to=r["to"],
                kept=", ".join(r["kept"]) or "-",
                stripped=", ".join(r["stripped"]) or "-"))
    return "\n".join(lines) + "\n"


def benchmark(n_chains=10_000, seed=42, max_hops=20):
    rng = random.Random(seed)
    script = {}
    plans = []
    for i in range(n_chains):
        kind = rng.choices(
            ["ok", "loop", "long", "invalid", "cross"],
            weights=[60, 10, 10, 10, 10])[0]
        depth = rng.randint(1, 30)
        base = f"https://h{i}.bench"
        if kind == "ok":
            for d in range(depth):
                script[f"{base}/{d}"] = (302, f"{base}/{d + 1}")
            script[f"{base}/{depth}"] = (200, None)
        elif kind == "loop":
            for d in range(depth):
                script[f"{base}/{d}"] = (302, f"{base}/{(d + 1) % depth}")
        elif kind == "long":
            for d in range(200):
                script[f"{base}/{d}"] = (302, f"{base}/{d + 1}")
        elif kind == "invalid":
            for d in range(depth):
                script[f"{base}/{d}"] = (302, f"{base}/{d + 1}")
            script[f"{base}/{depth}"] = (302, "javascript:alert(1)")
        else:  # cross-protocol mid-chain
            half = max(1, depth // 2)
            for d in range(half):
                script[f"{base}/{d}"] = (302, f"{base}/{d + 1}")
            script[f"{base}/{half}"] = (302, f"http://h{i}.bench/{half + 1}")
            for d in range(half + 1, depth):
                script[f"http://h{i}.bench/{d}"] = (
                    302, f"http://h{i}.bench/{d + 1}")
            script[f"http://h{i}.bench/{depth}"] = (200, None)
        plans.append((kind, f"{base}/0"))

    fetcher = ScriptedFetcher(script)
    tracer = RedirectTracer(fetcher, max_hops=max_hops)
    depths, times = [], []
    status_counts = {}
    t0 = time.perf_counter()
    for kind, start in plans:
        s = time.perf_counter()
        result = tracer.trace(start, AUTH)
        times.append(time.perf_counter() - s)
        depths.append(result.depth)
        status_counts[result.status] = status_counts.get(result.status, 0) + 1
    total = time.perf_counter() - t0

    sorted_times = sorted(times)
    return {
        "chains": n_chains,
        "max_hops_limit": max_hops,
        "total_seconds": round(total, 4),
        "per_chain_us": {
            "mean": round(statistics.mean(times) * 1e6, 2),
            "p50": round(sorted_times[len(times) // 2] * 1e6, 2),
            "p95": round(sorted_times[int(len(times) * 0.95)] * 1e6, 2),
            "max": round(max(times) * 1e6, 2),
        },
        "depth": {
            "max": max(depths),
            "mean": round(statistics.mean(depths), 2),
        },
        "terminal_status_counts": status_counts,
    }


def main():
    OUT.mkdir(exist_ok=True)

    records = run_scenarios()
    (OUT / "chain_records.json").write_text(
        json.dumps(records, indent=2, ensure_ascii=False))

    rows = policy_matrix()
    (OUT / "policy_matrix.md").write_text(write_policy_md(rows))
    (OUT / "policy_matrix.json").write_text(json.dumps(rows, indent=2))

    perf = benchmark()
    (OUT / "perf.json").write_text(json.dumps(perf, indent=2))

    print("=== scenario outcomes ===")
    for name, rec in records.items():
        print(f"  {name:32s} -> {rec['status']:18s} depth={rec['depth']}")
    print("\n=== benchmark (10k chains) ===")
    print(json.dumps(perf, indent=2))
    print(f"\nartifacts written to {OUT}/")


if __name__ == "__main__":
    main()
