"""Self-tests and step-by-step invariant checks for IndexedMinHeap."""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from indexed_priority_queue import IndexedMinHeap, UpdateReport


def heap_snapshot(heap: IndexedMinHeap[str, int]) -> str:
    return "[" + ", ".join(
        f"{item}:{heap.get_key(item)}@{heap.position(item)}" for item in heap
    ) + "]"


def trace_scenario(lines: list[str]) -> None:
    heap: IndexedMinHeap[str, int] = IndexedMinHeap()
    lines.append("== 逐步更新路径与不变量校验 ==")

    initial = [
        ("A", 5),
        ("B", 9),
        ("C", 7),
        ("D", 12),
        ("E", 3),
        ("F", 10),
        ("G", 8),
        ("H", 6),
    ]
    for item, key in initial:
        heap.push(item, key)
        heap.check_invariants()
        lines.append(
            f"push {item}:{key} -> 不变量 OK; heap={heap_snapshot(heap)}"
        )

    updates = [
        ("D", 1, "up"),
        ("A", 15, "down"),
        ("B", 9, "none"),
        ("H", -10**12, "up"),
        ("E", 10**12, "down"),
    ]
    for item, key, expected_direction in updates:
        report = heap.update_key(item, key, trace=True)
        assert isinstance(report, UpdateReport)
        heap.check_invariants()
        assert report.direction == expected_direction
        assert heap.position(item) == report.final_index
        assert heap.get_key(item) == key
        swap_text = ", ".join(
            f"{swap.from_index}->{swap.to_index}" for swap in report.swaps
        ) or "无交换"
        lines.append(
            f"update {item}: {report.old_key}->{report.new_key}; "
            f"定位 start={report.start_index}; 路径={report.direction}; "
            f"final={report.final_index}; swaps={swap_text}; 不变量 OK; "
            f"heap={heap_snapshot(heap)}"
        )

    lines.append("逐步场景结果: PASS")


def equal_key_scenario(lines: list[str]) -> None:
    heap = IndexedMinHeap[str, int](
        [("x", 4), ("y", 4), ("z", 4), ("w", 4)]
    )
    before = {item: heap.position(item) for item in heap}
    report = heap.update_key("y", 4, trace=True)
    after = {item: heap.position(item) for item in heap}

    heap.check_invariants()
    assert report is not None
    assert report.direction == "none"
    assert before == after
    assert len(heap) == 4
    assert len(set(heap)) == 4
    assert [heap.pop()[0] for _ in range(4)] == ["x", "y", "z", "w"]
    heap.check_invariants()

    lines.append("== 键相等 / 更新为同值 ==")
    lines.append(
        "同值更新 y:4->4: direction=none, 4 个元素位置不变, "
        "无丢失/重复, 弹出顺序=x,y,z,w: PASS"
    )


def monotonic_scenarios(lines: list[str]) -> None:
    decrease_heap = IndexedMinHeap[int, int]((i, 10_000 + i) for i in range(64))
    for step in range(64):
        item = 63 - step
        decrease_heap.update_key(item, step)
        decrease_heap.check_invariants()
    assert decrease_heap.peek() == (63, 0)
    lines.append("只减键场景: 64/64 次更新后不变量 OK: PASS")

    increase_heap = IndexedMinHeap[int, int]((i, i) for i in range(64))
    for step in range(64):
        item = step
        increase_heap.update_key(item, 1_000 + step)
        increase_heap.check_invariants()
    assert increase_heap.peek() == (0, 1_000)
    lines.append("只增键场景: 64/64 次更新后不变量 OK: PASS")


def edge_scenarios(lines: list[str]) -> None:
    heap = IndexedMinHeap[str, float | int](
        [("low", 0.0), ("high", 10.0), ("mid", 5.0)]
    )
    heap.update_key("mid", float("-inf"))
    heap.check_invariants()
    assert heap.peek()[0] == "mid"
    heap.update_key("mid", float("inf"))
    heap.check_invariants()
    assert heap.pop()[0] == "low"
    assert heap.pop()[0] == "high"
    assert heap.pop() == ("mid", float("inf"))

    try:
        heap.update_key("missing", 1)
    except KeyError:
        missing_ok = True
    else:
        missing_ok = False

    duplicate_heap = IndexedMinHeap[str, int]()
    duplicate_heap.push("a", 1)
    try:
        duplicate_heap.push("a", 2)
    except ValueError:
        duplicate_ok = True
    else:
        duplicate_ok = False

    try:
        duplicate_heap.update_key("a", float("nan"))
    except ValueError:
        nan_ok = True
    else:
        nan_ok = False

    assert missing_ok and duplicate_ok and nan_ok
    lines.append("极端值/缺失元素/重复元素/NaN 边界: PASS")


def small_fuzz_scenario(lines: list[str]) -> None:
    rng = random.Random(340)
    heap = IndexedMinHeap[int, int]()
    mirror: dict[int, int] = {}

    for step in range(300):
        action = rng.randrange(3)
        if action == 0 or not mirror:
            item = step
            key = rng.randrange(-100, 101)
            heap.push(item, key)
            mirror[item] = key
        elif action == 1:
            item = rng.choice(tuple(mirror))
            key = rng.randrange(-100, 101)
            heap.update_key(item, key)
            mirror[item] = key
        else:
            item, key = heap.pop()
            assert mirror.pop(item) == key
            assert all(key <= other for other in mirror.values())

        heap.check_invariants()
        assert len(heap) == len(mirror)
        for item, key in mirror.items():
            assert heap.get_key(item) == key

    lines.append("随机小步数自测: 300/300 次操作后不变量与镜像一致: PASS")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("invariant_report.txt"),
        help="where to write the human-readable invariant report",
    )
    args = parser.parse_args()

    lines: list[str] = []
    trace_scenario(lines)
    equal_key_scenario(lines)
    monotonic_scenarios(lines)
    edge_scenarios(lines)
    small_fuzz_scenario(lines)
    lines.append("SELF-TEST RESULT: PASS")

    text = "\n".join(lines) + "\n"
    args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
