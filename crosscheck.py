"""对拍脚本：生成确定性数据集，双实现互拍，导出回退结果与差异说明。

运行：python3 crosscheck.py
产物：data/*.jsonl（对拍输入）、data/*_sorted.jsonl（排序结果）、
      data/fallback_results.txt（回退结果）、data/explain_report.txt（差异说明）

对拍方法：
  实现 A = stable_sort（一次成型，键元组 + 隐式原始下标）
  实现 B = multipass_sort（从最低级键开始逐级稳定排序）
  两者算法路径完全不同；对每个数据集要求输出逐位一致（按对象 id）。
"""

import json
import random
import sys
from pathlib import Path

from stable_sort_pkg import SortKey, stable_sort, multipass_sort, explain_vs_primary

DATA = Path(__file__).parent / "data"


def dump_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def make_all_ties(n=200):
    return [{"id": i, "a": 1, "b": "same"} for i in range(n)]


def make_no_ties(n=200):
    rng = random.Random(11)
    values = rng.sample(range(10_000), n)
    return [{"id": i, "a": v, "b": i % 7} for i, v in enumerate(values)]


def make_partial_missing(n=200):
    rng = random.Random(22)
    rows = []
    for i in range(n):
        row = {"id": i, "a": rng.randrange(0, 5)}
        r = rng.random()
        if r < 0.25:
            pass  # b 字段整体缺失
        elif r < 0.4:
            row["b"] = None  # 显式 None
        else:
            row["b"] = rng.randrange(0, 10)
        rows.append(row)
    return rows


def make_incomparable(n=200):
    rng = random.Random(33)
    rows = []
    for i in range(n):
        row = {"id": i}
        r = rng.random()
        if r < 0.35:
            row["a"] = rng.randrange(0, 20)
        elif r < 0.7:
            row["a"] = rng.choice(["apple", "banana", "cherry", "date"])
        else:
            pass  # 缺失
        rows.append(row)
    return rows


def make_mixed(n=2000):
    rng = random.Random(44)
    rows = []
    for i in range(n):
        row = {"id": i, "k1": rng.randrange(0, 8)}
        if rng.random() < 0.8:
            row["k2"] = rng.choice(["x", "y", "z"])
        if rng.random() < 0.9:
            row["k3"] = rng.randrange(0, 4)
        if rng.random() < 0.05:
            row["k3"] = "str-instead"  # 混入不可比类型
        rows.append(row)
    return rows


DATASETS = {
    "all_ties": (make_all_ties, [SortKey("a"), SortKey("b")]),
    "no_ties": (make_no_ties, [SortKey("a"), SortKey("b", descending=True)]),
    "partial_missing": (
        make_partial_missing,
        [SortKey("a"), SortKey("b", nulls="last")],
    ),
    "incomparable": (make_incomparable, [SortKey("a"), SortKey("id")]),
    "mixed": (
        make_mixed,
        [SortKey("k1"), SortKey("k2", nulls="first"),
         SortKey("k3", descending=True)],
    ),
}


def main():
    DATA.mkdir(exist_ok=True)
    failures = 0
    generated = {}

    for name, (maker, keys) in DATASETS.items():
        rows = maker()
        generated[name] = (rows, keys)
        dump_jsonl(DATA / f"{name}.jsonl", rows)

        result_a = stable_sort(rows, keys)
        result_b = multipass_sort(rows, keys)
        ids_a = [id(r) for r in result_a]
        ids_b = [id(r) for r in result_b]
        ok = ids_a == ids_b
        # 再跑两遍验证重复排序逐位一致
        repeat_ok = all(
            [id(r) for r in stable_sort(rows, keys)] == ids_a for _ in range(3)
        )
        # 无丢失无重复
        perm_ok = sorted(r["id"] for r in result_a) == sorted(
            r["id"] for r in rows
        )
        status = "OK" if (ok and repeat_ok and perm_ok) else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"[{status}] {name:16s} n={len(rows):5d} "
              f"A==B:{ok} 重复一致:{repeat_ok} 无丢失重复:{perm_ok}")
        dump_jsonl(DATA / f"{name}_sorted.jsonl", result_a)

    _write_fallback_results(generated)
    _write_explain_report(generated["mixed"][0], DATASETS["mixed"][1])

    if failures:
        print(f"\n{failures} 个数据集对拍失败", file=sys.stderr)
        sys.exit(1)
    print("\n全部数据集对拍通过；产物见 data/ 目录")


def _bucket_of(keys, row):
    """标注每行在第一级键下落入的桶，用于回退结果说明。"""
    from stable_sort_pkg.stable import _make_term
    term = _make_term(keys[0], keys[0].extract(row))
    bucket, category = term[0], term[1]
    return {0: "present(可比值)", 1: "missing(缺失/None)",
            2: "incomparable(不可比)"}[category]


def _write_fallback_results(generated):
    lines = []
    lines.append("回退结果（确定性回退的实际输出）")
    lines.append("=" * 60)

    for name in ("partial_missing", "incomparable"):
        rows, keys = generated[name]
        result = stable_sort(rows, keys)
        lines.append("")
        lines.append(f"数据集 {name}（前 12 条排序结果，含桶标注）")
        lines.append(f"排序键: {[ (k.field, k.descending, k.nulls) for k in keys ]}")
        for row in result[:12]:
            lines.append(f"  id={row['id']:4d} a={row.get('a')!r:10} "
                         f"bucket={_bucket_of(keys, row)}")
        lines.append("规则：present < missing < incomparable（nulls='last'）；")
        lines.append("missing 桶内一律相等 -> 由下一级键、最终由原始下标裁决；")
        lines.append("incomparable 桶内按 (模块名, 类型名, repr) 全序键排序。")

    # 专门演示跨类型回退的确定顺序
    demo = [{"id": 0, "a": 10}, {"id": 1, "a": "b"},
            {"id": 2, "a": 2}, {"id": 3, "a": "a"}]
    asc = stable_sort(demo, [SortKey("a")])
    desc = stable_sort(demo, [SortKey("a", descending=True)])
    lines.append("")
    lines.append("跨类型回退演示（int 与 str 混排）")
    lines.append(f"  升序: {[ (r['id'], r['a']) for r in asc ]}")
    lines.append(f"  降序: {[ (r['id'], r['a']) for r in desc ]}")
    lines.append("  说明：同类型按自然序，跨类型按 (builtins, int|str, repr)，")
    lines.append("  降序反转该级完整全序（含回退序）。")

    text = "\n".join(lines) + "\n"
    (DATA / "fallback_results.txt").write_text(text, encoding="utf-8")
    print("已写出 data/fallback_results.txt")


def _write_explain_report(rows, keys):
    report = explain_vs_primary(rows, keys)
    lines = [
        "多级排序 vs 只按主键排序 —— 差异说明",
        "=" * 60,
        f"记录总数: {report['total']}",
        f"主键等值组数量(组大小>1): {report['tie_group_count']}",
        f"等值组大小分布(组大小:组数): {report['tie_group_size_distribution']}",
        f"组内位置发生变化的记录数: {report['moved_within_primary_tie_groups']}",
        "",
        report["interpretation"],
        "",
        "等值组样例（最多 5 个）:",
    ]
    for ex in report["examples"]:
        lines.append(f"  主键值={ex['primary_value']} 组大小={ex['size']} "
                     f"多级结果中连续={ex['contiguous_in_multi']}")
        moves = ex["member_moves_primary_to_multi"][:6]
        lines.append(f"    组内重排样例(主键序位置->多级序位置): {moves}")
    (DATA / "explain_report.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    print("已写出 data/explain_report.txt")


if __name__ == "__main__":
    main()
