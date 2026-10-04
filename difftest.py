"""对拍：排序合并 merge_sorted vs 逐对两两合并 merge_pairwise。

生成 data/difftest_cases.jsonl（代表性用例明细）并在 stdout 打印汇总。
"""

import json
import random

from interval_merge import Interval, merge_pairwise, merge_sorted


def case_record(name, ivs):
    fast = merge_sorted(ivs)
    ref = merge_pairwise(ivs)
    return {
        "name": name,
        "input": [[iv.start, iv.end] for iv in ivs],
        "merge_sorted": [[iv.start, iv.end] for iv in fast],
        "merge_pairwise": [[iv.start, iv.end] for iv in ref],
        "match": fast == ref,
    }


def main():
    records = [
        case_record("empty", []),
        case_record("single_point", [Interval(5, 5)]),
        case_record("disjoint", [Interval(0, 10), Interval(20, 30)]),
        case_record("touching", [Interval(0, 10), Interval(10, 20)]),
        case_record("contained", [Interval(0, 100), Interval(10, 50)]),
        case_record("partial_overlap", [Interval(0, 15), Interval(10, 20)]),
        case_record("zero_length_inside", [Interval(0, 10), Interval(5, 5)]),
        case_record("zero_length_touching", [Interval(0, 10), Interval(10, 10)]),
        case_record("two_distinct_points", [Interval(7, 7), Interval(9, 9)]),
        case_record("point_chain", [Interval(0, 0), Interval(0, 5), Interval(5, 5)]),
        case_record("unsorted_mixed",
                    [Interval(20, 30), Interval(0, 10), Interval(5, 25),
                     Interval(40, 40), Interval(28, 40)]),
    ]

    rng = random.Random(20261005)
    n_random = 2000
    mismatches = 0
    for t in range(n_random):
        n = rng.randint(0, 40)
        ivs = [Interval(a := rng.randint(0, 200), a + rng.randint(0, 30))
               for _ in range(n)]
        fast, ref = merge_sorted(ivs), merge_pairwise(ivs)
        if fast != ref:
            mismatches += 1
            records.append(case_record(f"random_mismatch_{t}", ivs))

    with open("data/difftest_cases.jsonl", "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"代表性用例: {len(records)} 条 -> data/difftest_cases.jsonl")
    print(f"随机对拍: {n_random} 组, 不一致 {mismatches} 组")
    print("全部一致" if mismatches == 0 and all(r["match"] for r in records)
          else "存在不一致，请检查！")


if __name__ == "__main__":
    main()
