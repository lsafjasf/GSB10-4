"""1 万条重定向链追踪基准：耗时与最大深度统计。

运行: python3 benchmark.py
输出: out/benchmark.json + 终端摘要
"""

import json
import time

from redirect_tracer import RedirectTracer

N_CHAINS = 10_000
MAX_DEPTH = 50  # 链深在 1..MAX_DEPTH 间轮转


def build_routes():
    routes = {}
    for i in range(N_CHAINS):
        depth = (i % MAX_DEPTH) + 1
        for j in range(depth):
            cur = f"http://bench{i}.example.com/hop{j}"
            nxt = f"http://bench{i}.example.com/hop{j + 1}"
            routes[cur] = (302, {"Location": nxt})
        routes[f"http://bench{i}.example.com/hop{depth}"] = (200, {})
    return routes


def main():
    routes = build_routes()
    tracer = RedirectTracer(lambda url, headers: routes[url],
                            max_redirects=MAX_DEPTH + 5)
    headers = {"Authorization": "Bearer s3cret", "Cookie": "sid=abc"}

    start = time.perf_counter()
    depths = []
    reasons = {}
    for i in range(N_CHAINS):
        result = tracer.trace(f"http://bench{i}.example.com/hop0", headers=headers)
        depths.append(result.depth)
        reasons[result.reason] = reasons.get(result.reason, 0) + 1
    elapsed = time.perf_counter() - start

    total_hops = sum(depths) + N_CHAINS
    stats = {
        "chains": N_CHAINS,
        "total_seconds": round(elapsed, 4),
        "avg_ms_per_chain": round(elapsed / N_CHAINS * 1000, 4),
        "chains_per_second": round(N_CHAINS / elapsed),
        "total_hops": total_hops,
        "max_depth": max(depths),
        "min_depth": min(depths),
        "avg_depth": round(sum(depths) / len(depths), 2),
        "reasons": reasons,
    }
    with open("out/benchmark.json", "w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
