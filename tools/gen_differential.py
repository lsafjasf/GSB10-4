"""生成打乱顺序的对拍数据（固定随机种子，结果可复现）。

对每个用例用当前库实现算出“金标准”结果，连同输入一起写入
data/differential_cases.json；自测时以多种行序/交换操作数顺序重算，
与金标准逐行比对，验证结果与运算顺序无关。
"""
from __future__ import annotations

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setops import Dataset, Type, join
from setops.encoding import decode_row, encode_row
from tools.pools import sample_row

SEED = 20261005
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "differential_cases.json")

ALL_OPS = ("union", "intersection", "difference")


def _run(left_types, right_types, left_rows, right_rows, op):
    a = Dataset(left_types, left_rows, name="left")
    b = Dataset(right_types, right_rows, name="right")
    try:
        result = getattr(a, op)(b)
    except Exception as exc:  # noqa: BLE001 - 金标准记录异常类别与位置
        return {
            "kind": "error",
            "error": type(exc).__name__,
            "column": getattr(exc, "column", None),
            "message": str(exc),
        }
    rows = sorted((encode_row(r) for r in result.rows),
                  key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False))
    return {
        "kind": "ok",
        "types": [t.value for t in result.types],
        "rows": rows,
        "precision_loss": result.precision.total_loss_count,
        "precision": [
            {
                "column": e.column,
                "src": e.src.value,
                "dst": e.dst.value,
                "count": e.count,
            }
            for e in result.precision.entries
        ],
    }


def build_cases():
    rng = random.Random(SEED)
    cases = []

    # (名称, 左类型, 右类型, 左行数, 右行数, 空值率, 运算)
    plans = []
    # —— 同类型 ——
    for t in Type:
        if t is Type.NULL:
            continue
        plans.append((f"same-{t.value}", [t], [t], 40, 40, 0.0))
    # —— 可提升 ——
    promotable_pairs = [
        (Type.BOOL, Type.INT32),
        (Type.INT32, Type.INT64),
        (Type.INT64, Type.BIGINT),
        (Type.INT32, Type.FLOAT32),
        (Type.BOOL, Type.FLOAT32),
        (Type.INT32, Type.FLOAT64),
        (Type.INT64, Type.FLOAT32),
        (Type.INT64, Type.FLOAT64),
        (Type.BIGINT, Type.FLOAT64),
        (Type.INT64, Type.DECIMAL),
        (Type.BIGINT, Type.DECIMAL),
        (Type.FLOAT32, Type.FLOAT64),
        (Type.DECIMAL, Type.FLOAT64),
        (Type.DATE, Type.DATETIME),
    ]
    for i, (a, b) in enumerate(promotable_pairs):
        plans.append((f"promote-{a.value}-{b.value}", [a], [b], 40, 40, 0.0))
        plans.append((f"promote-rev-{a.value}-{b.value}", [b], [a], 40, 40, 0.0))
    # —— 不可比 ——
    incomparable_pairs = [
        (Type.STRING, Type.INT64),
        (Type.INT32, Type.BYTES),
        (Type.STRING, Type.BYTES),
        (Type.DECIMAL, Type.STRING),
        (Type.DATE, Type.INT64),
        (Type.DATETIME, Type.STRING),
        (Type.BYTES, Type.DATETIME),
    ]
    for a, b in incomparable_pairs:
        plans.append((f"incomparable-{a.value}-{b.value}", [a], [b], 10, 10, 0.0))
    # —— 含空值（NULL 类型列 + 普通列里的 None） ——
    plans.append(("null-column-vs-int", [Type.NULL], [Type.INT32], 20, 20, 0.0))
    plans.append(("nulls-int64-float64", [Type.INT64], [Type.FLOAT64], 60, 60, 0.15))
    plans.append(("nulls-string", [Type.STRING], [Type.STRING], 40, 40, 0.2))
    plans.append(("nulls-date-datetime", [Type.DATE], [Type.DATETIME], 40, 40, 0.2))
    # —— 多列混合（第 2 列可提升、第 3 列不可比，用于验证报错位置） ——
    plans.append(("multi-ok", [Type.INT32, Type.STRING, Type.DATE],
                  [Type.FLOAT64, Type.STRING, Type.DATETIME], 30, 30, 0.1))
    plans.append(("multi-bad-col2", [Type.INT32, Type.STRING],
                  [Type.FLOAT64, Type.INT64], 10, 10, 0.0))
    plans.append(("multi-bad-col3", [Type.INT64, Type.STRING, Type.BYTES],
                  [Type.BIGINT, Type.STRING, Type.INT32], 10, 10, 0.0))

    for name, lt, rt, nl, nr, null_rate in plans:
        left_rows = [sample_row(lt, rng, null_rate) for _ in range(nl)]
        right_rows = [sample_row(rt, rng, null_rate) for _ in range(nr)]
        for op in ALL_OPS:
            golden = _run(lt, rt, left_rows, right_rows, op)
            cases.append({
                "name": f"{name}-{op}",
                "op": op,
                "left_types": [t.value for t in lt],
                "right_types": [t.value for t in rt],
                "left_rows": [encode_row(r) for r in left_rows],
                "right_rows": [encode_row(r) for r in right_rows],
                "expected": golden,
            })
    return cases


def main():
    cases = build_cases()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"seed": SEED, "case_count": len(cases), "cases": cases},
                  f, ensure_ascii=False, indent=1)
    ok = sum(1 for c in cases if c["expected"]["kind"] == "ok")
    bad = len(cases) - ok
    loss = sum(c["expected"].get("precision_loss", 0) for c in cases)
    print(f"已生成 {len(cases)} 个对拍用例（成功类 {ok}，拒绝类 {bad}，"
          f"累计精度丢失值 {loss}）-> {OUT}")


if __name__ == "__main__":
    main()
