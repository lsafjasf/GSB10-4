"""对拍（differential）测试：同一批数据打乱集合顺序与行顺序，
结果（统一类型 / 值序列 / 精度计数 / 报错类型对）必须完全一致。

运行：python3 tests/differential.py
产物：data/differential_cases.json（输入、各打乱变体、各自结果摘要）
"""

import hashlib
import json
import os
import random
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from typeunify import TypeUnificationError, difference, intersection, union

OPS = {"union": union, "intersection": intersection, "difference": difference}

CASES = [
    {"name": "same_type_int", "op": "union",
     "collections": [[1, 2, 2, 3], [3, 4, 5], [5, 6]]},
    {"name": "same_type_string", "op": "intersection",
     "collections": [["a", "b", "c"], ["b", "c", "d"], ["c", "b"]]},
    {"name": "promotable_bool_int_float", "op": "union",
     "collections": [[True, False, 7], [2.5, 1], [0, 2.5]]},
    {"name": "promotable_int_decimal", "op": "intersection",
     "collections": [[1, 2, 3], [Decimal("2"), Decimal("3.5")]]},
    {"name": "promotable_float_decimal", "op": "union",
     "collections": [[0.5, 1.5], [Decimal("2.25"), 1.5]]},
    {"name": "with_nulls", "op": "union",
     "collections": [[None, 1, None], [2, None], [None, 3.5]]},
    {"name": "all_nulls", "op": "union",
     "collections": [[None, None], [None]]},
    {"name": "difference_mixed", "op": "difference",
     "collections": [[1, 2, 3, True, None], [2.0], [None, 10]]},
    {"name": "incomparable_str_int", "op": "union",
     "collections": [["a", "b"], [1, 2], ["c"]]},
    {"name": "incomparable_bytes_float", "op": "intersection",
     "collections": [[b"x", 1.5], [2.5]]},
    {"name": "precision_big_int_float", "op": "union",
     "collections": [[2 ** 53 + 1, 8], [0.5, 2 ** 53 + 3]]},
    {"name": "precision_decimal_float", "op": "union",
     "collections": [[Decimal("0.1"), Decimal("0.5")], [1.0]]},
]

N_SHUFFLES = 25
SEED = 20261007
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "differential_cases.json")


def encode(value):
    if value is None:
        return {"t": "null"}
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        return {"t": "int", "v": str(value)}
    if isinstance(value, float):
        return {"t": "float", "v": repr(value)}
    if isinstance(value, Decimal):
        return {"t": "decimal", "v": str(value)}
    if isinstance(value, str):
        return {"t": "string", "v": value}
    if isinstance(value, (bytes, bytearray)):
        return {"t": "bytes", "v": value.hex()}
    raise TypeError(type(value))


def decode(d):
    t = d["t"]
    if t == "null":
        return None
    if t == "bool":
        return bool(d["v"])
    if t == "int":
        return int(d["v"])
    if t == "float":
        return float(d["v"])
    if t == "decimal":
        return Decimal(d["v"])
    if t == "string":
        return d["v"]
    if t == "bytes":
        return bytes.fromhex(d["v"])
    raise ValueError(t)


def run_case(op_name, collections):
    """返回确定性摘要：成功为结果摘要，失败为报错摘要。"""
    op = OPS[op_name]
    try:
        r = op(*collections)
    except TypeUnificationError as e:
        return {"status": "error", "type_a": e.type_a.name, "type_b": e.type_b.name}
    return {
        "status": "ok",
        "unified_type": r.unified_type.name,
        "values": [encode(v) for v in r.values],
        "precision": {"%s>%s" % k: n for k, n in sorted(r.precision.counts.items())},
    }


def digest(summary):
    blob = json.dumps(summary, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def main():
    rng = random.Random(SEED)
    out_cases = []
    mismatches = []

    for case in CASES:
        base = case["collections"]
        variants = []
        for _ in range(N_SHUFFLES):
            shuffled = [list(c) for c in base]
            for c in shuffled:
                rng.shuffle(c)
            if case["op"] in ("union", "intersection"):
                rng.shuffle(shuffled)  # 交换律成立，集合顺序也应无关
            variants.append(shuffled)

        ref = run_case(case["op"], base)
        ref_digest = digest(ref)
        variant_records = []
        for i, variant in enumerate(variants):
            got = run_case(case["op"], variant)
            got_digest = digest(got)
            ok = got_digest == ref_digest
            if not ok:
                mismatches.append((case["name"], i))
            variant_records.append({
                "variant": i,
                "collections": [[encode(v) for v in c] for c in variant],
                "digest": got_digest,
                "matches_reference": ok,
            })

        out_cases.append({
            "name": case["name"],
            "op": case["op"],
            "base_collections": [[encode(v) for v in c] for c in base],
            "reference_digest": ref_digest,
            "reference_result": ref,
            "variants": variant_records,
        })

    payload = {
        "seed": SEED,
        "shuffles_per_case": N_SHUFFLES,
        "cases": out_cases,
        "all_match": not mismatches,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    total = len(CASES) * (N_SHUFFLES + 1)
    print("cases: %d, runs: %d, mismatches: %d" % (len(CASES), total, len(mismatches)))
    for name, i in mismatches:
        print("MISMATCH: %s variant %d" % (name, i))
    print("written: %s" % os.path.normpath(OUT_PATH))
    sys.exit(1 if mismatches else 0)


if __name__ == "__main__":
    main()
