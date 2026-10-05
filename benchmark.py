"""Benchmark one million IndexedMinHeap operations using only the stdlib."""
from __future__ import annotations
import argparse
import json
import platform
import random
import resource
import time
import tracemalloc
from pathlib import Path
from indexed_priority_queue import IndexedMinHeap

def cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"

def max_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

def run_benchmark(operations: int, workload: str, seed: int, measure_python_memory: bool) -> dict:
    rng = random.Random(seed)
    heap = IndexedMinHeap[int, int]()
    live_items: list[int] = []
    live_positions: dict[int, int] = {}
    next_item = 0
    if measure_python_memory:
        tracemalloc.start()
    setup_start = time.perf_counter()
    initial_items = operations + 1 if workload == "update-only" else 10_000
    for _ in range(initial_items):
        heap.push(next_item, rng.randrange(-10**9, 10**9 + 1))
        live_positions[next_item] = len(live_items)
        live_items.append(next_item)
        next_item += 1
    setup_seconds = time.perf_counter() - setup_start
    counts = {"push": 0, "pop": 0, "update": 0, "update_up": 0, "update_down": 0, "update_equal": 0}
    start = time.perf_counter()
    for _ in range(operations):
        if workload == "update-only":
            action = "update"
        else:
            roll = rng.random()
            if roll < 0.85:
                action = "update"
            elif roll < 0.925:
                action = "push"
            else:
                action = "pop"
        if action == "update":
            live_index = rng.randrange(len(live_items))
            item = live_items[live_index]
            old_key = heap.get_key(item)
            new_key = rng.randrange(-10**9, 10**9 + 1)
            heap.update_key(item, new_key)
            counts["update"] += 1
            if new_key < old_key:
                counts["update_up"] += 1
            elif new_key > old_key:
                counts["update_down"] += 1
            else:
                counts["update_equal"] += 1
        elif action == "push" or not live_items:
            item = next_item
            next_item += 1
            heap.push(item, rng.randrange(-10**9, 10**9 + 1))
            live_positions[item] = len(live_items)
            live_items.append(item)
            counts["push"] += 1
        else:
            item, _key = heap.pop()
            live_index = live_positions.pop(item)
            last_item = live_items.pop()
            if live_index < len(live_items):
                live_items[live_index] = last_item
                live_positions[last_item] = live_index
            counts["pop"] += 1
    operation_seconds = time.perf_counter() - start
    heap.check_invariants()
    python_peak_mib = None
    if measure_python_memory:
        _current, peak = tracemalloc.get_traced_memory()
        python_peak_mib = peak / (1024 * 1024)
        tracemalloc.stop()
    return {
        "workload": workload,
        "seed": seed,
        "operations": operations,
        "initial_items": initial_items,
        "final_items": len(heap),
        "operation_counts": counts,
        "setup_seconds": round(setup_seconds, 6),
        "operation_seconds": round(operation_seconds, 6),
        "operations_per_second": round(operations / operation_seconds, 2),
        "max_rss_mib": round(max_rss_mib(), 3),
        "python_allocated_peak_mib": round(python_peak_mib, 3) if python_peak_mib is not None else None,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu": cpu_model(),
        "invariant_after_workload": "PASS",
    }

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operations", type=int, default=1_000_000)
    parser.add_argument("--workload", choices=("mixed", "update-only"), default="mixed")
    parser.add_argument("--seed", type=int, default=340)
    parser.add_argument("--memory", action="store_true", help="enable tracemalloc peak measurement")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_benchmark(args.operations, args.workload, args.seed, args.memory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
