"""Differential testing against a heap rebuilt after every operation."""

from __future__ import annotations

import argparse
import csv
import heapq
import json
import random
from pathlib import Path

from indexed_priority_queue import IndexedMinHeap


class NaiveRebuildHeap:
    """Reference heap: store entries and rebuild the heap after each op."""

    def __init__(self) -> None:
        self._entries: dict[int, tuple[int, int]] = {}
        self._heap: list[tuple[int, int, int]] = []
        self._next_rank = 0

    def __len__(self) -> int:
        return len(self._entries)

    def _rebuild(self) -> None:
        self._heap = [
            (key, rank, item)
            for item, (key, rank) in self._entries.items()
        ]
        heapq.heapify(self._heap)

    def push(self, item: int, key: int) -> None:
        if item in self._entries:
            raise ValueError
        self._entries[item] = (key, self._next_rank)
        self._next_rank += 1
        self._rebuild()

    def update_key(self, item: int, key: int) -> None:
        old_key, rank = self._entries[item]
        self._entries[item] = (key if key != old_key else old_key, rank)
        self._rebuild()

    def pop(self) -> tuple[int, int]:
        key, _rank, item = heapq.heappop(self._heap)
        del self._entries[item]
        self._rebuild()
        return item, key

    def peek(self) -> tuple[int, int]:
        key, _rank, item = self._heap[0]
        return item, key


def run(operations: int, seed: int, trace_path: Path, summary_path: Path) -> dict:
    rng = random.Random(seed)
    indexed = IndexedMinHeap[int, int]()
    naive = NaiveRebuildHeap()

    push_count = update_count = pop_count = equal_update_count = 0

    with trace_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "step",
                "operation",
                "item",
                "old_key",
                "new_key",
                "size_after",
                "our_min",
                "naive_min",
                "naive_rebuilt",
                "result",
            ]
        )

        for step in range(operations):
            choice = rng.random()
            item = -1
            old_key = ""
            new_key = ""
            operation = ""

            if choice < 0.40 or len(indexed) == 0:
                operation = "push"
                item = step
                key = rng.randrange(-1000, 1001)
                new_key = key
                indexed.push(item, key)
                naive.push(item, key)
                push_count += 1
            elif choice < 0.85:
                operation = "update"
                item = rng.choice(tuple(indexed))
                old_key = indexed.get_key(item)
                key = old_key if rng.random() < 0.10 else rng.randrange(-1000, 1001)
                new_key = key
                if key == old_key:
                    equal_update_count += 1
                indexed.update_key(item, key)
                naive.update_key(item, key)
                update_count += 1
            else:
                operation = "pop"
                our_item, our_key = indexed.pop()
                naive_item, naive_key = naive.pop()
                item = our_item
                old_key = our_key
                new_key = naive_key
                assert (our_item, our_key) == (naive_item, naive_key)
                pop_count += 1

            indexed.check_invariants()
            assert len(indexed) == len(naive)
            our_min = indexed.peek() if indexed else None
            naive_min = naive.peek() if naive else None
            assert our_min == naive_min

            writer.writerow(
                [
                    step,
                    operation,
                    item,
                    old_key,
                    new_key,
                    len(indexed),
                    f"{our_min[0]}:{our_min[1]}" if our_min else "",
                    f"{naive_min[0]}:{naive_min[1]}" if naive_min else "",
                    "true",
                    "match",
                ]
            )

    our_sequence: list[tuple[int, int]] = []
    naive_sequence: list[tuple[int, int]] = []
    while indexed:
        our_sequence.append(indexed.pop())
        naive_sequence.append(naive.pop())
        indexed.check_invariants()

    assert our_sequence == naive_sequence

    summary = {
        "seed": seed,
        "operations": operations,
        "push_count": push_count,
        "update_count": update_count,
        "pop_count": pop_count,
        "equal_value_update_count": equal_update_count,
        "remaining_items": len(our_sequence),
        "min_after_every_operation": "match",
        "final_pop_sequence": "match",
        "result": "PASS",
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operations", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--trace", type=Path, default=Path("data/differential_trace.csv"))
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path("data/differential_summary.json"),
    )
    args = parser.parse_args()

    args.trace.parent.mkdir(parents=True, exist_ok=True)
    summary = run(args.operations, args.seed, args.trace, args.summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
