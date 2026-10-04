"""生成交付数据文件（data/ 目录）：

  crosscheck.json         对拍数据：手工场景 + 固定种子随机批，快速实现 vs 参考实现
  zero_length_results.json 零长度区间各情形的处理结果
  negative_results.json    负长度区间在 strict / normalize 两种策略下的结果
  tz_precision_diff.json   时区与精度统一前后的端点差异
  pre_post_merge_diff.json 统一前（naive 本地墙钟）与统一后（UTC）合并结果差异

运行：python3 gen_artifacts.py
"""

import json
import os
import random
from datetime import timezone

import imerge
from imerge import (
    merge_datetime_intervals,
    merge_intervals,
    merge_intervals_reference,
    normalize_datetime_endpoint,
    normalize_intervals,
    normalize_pairs,
)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def write(name, obj):
    path = os.path.join(DATA_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print("写出 %s" % path)


def run_case(pairs, merge_touching):
    fast = merge_intervals(pairs, merge_touching=merge_touching)
    ref = merge_intervals_reference(pairs, merge_touching=merge_touching)
    return {
        "input": [[s, e] for s, e in pairs],
        "merge_touching": merge_touching,
        "fast_result": [[s, e] for s, e in fast],
        "reference_result": [[s, e] for s, e in ref],
        "match": fast == ref,
    }


def gen_crosscheck():
    handcrafted = [
        ("互不相接", [(0, 1), (2, 3), (5, 8)]),
        ("恰好相接", [(0, 1), (1, 2), (2, 3)]),
        ("部分重叠", [(0, 2), (1, 3), (2, 4)]),
        ("完全包含", [(0, 10), (2, 5), (3, 4)]),
        ("包含者后出现", [(2, 5), (0, 10), (3, 4)]),
        ("零长度点-区间内", [(0, 10), (3, 3)]),
        ("零长度点-左端点", [(0, 10), (0, 0)]),
        ("零长度点-右端点", [(0, 10), (10, 10)]),
        ("零长度点-区间外", [(0, 1), (5, 5), (2, 2)]),
        ("相同点去重", [(5, 5), (5, 5), (5, 8)]),
        ("点不搭桥", [(1, 2), (2, 2), (2, 3)]),
        ("混合大场景", [(0, 3), (2, 4), (4, 4), (4, 6), (9, 9), (8, 10), (20, 22), (21, 21)]),
    ]
    cases = []
    for name, pairs in handcrafted:
        for mt in (False, True):
            c = run_case(pairs, mt)
            c["case"] = name
            c["kind"] = "handcrafted"
            cases.append(c)

    rng = random.Random(20261005)
    for i in range(200):
        n = rng.randint(0, 12)
        pairs = []
        for _ in range(n):
            a, b = rng.randint(0, 50), rng.randint(0, 50)
            if b < a:
                a, b = b, a
            pairs.append((a, b))
        for mt in (False, True):
            c = run_case(pairs, mt)
            c["case"] = "random-%03d" % i
            c["kind"] = "random"
            cases.append(c)

    mismatches = sum(1 for c in cases if not c["match"])
    write("crosscheck.json", {
        "description": "快速实现 merge_intervals 与逐对两两合并参考实现 "
                       "merge_intervals_reference 的对拍数据；match 全部为 true",
        "seed": 20261005,
        "total_cases": len(cases),
        "mismatches": mismatches,
        "cases": cases,
    })
    assert mismatches == 0, "对拍出现不一致！"
    return len(cases)


def gen_zero_length():
    scenarios = [
        ("点落在正长度区间内部（R5 吸收）", [(0, 10), (3, 3)]),
        ("点恰在区间左端点（R6 吸收）", [(0, 10), (0, 0)]),
        ("点恰在区间右端点（R7 不吸收，点保留）", [(0, 10), (10, 10)]),
        ("点在所有区间之外（保留为独立点）", [(0, 1), (5, 5)]),
        ("两个相同的点（去重为一个）", [(5, 5), (5, 5)]),
        ("两个不同的点（都保留）", [(5, 5), (7, 7)]),
        ("点夹在两相接区间之间（touch=False 时被右区间左端吸收）", [(1, 2), (2, 2), (2, 3)]),
        ("点夹在两相接区间之间（touch=True 时先合并再被吸收）", [(1, 2), (2, 2), (2, 3)]),
    ]
    out = []
    for i, (name, pairs) in enumerate(scenarios):
        mt = "touch=True" in name
        res = merge_intervals(pairs, merge_touching=mt)
        out.append({
            "scenario": name,
            "input": [[s, e] for s, e in pairs],
            "merge_touching": mt,
            "result": [[s, e] for s, e in res],
        })
    write("zero_length_results.json", {
        "description": "零长度区间（点）在各情形下的合并结果；规则编号见 RULES.md R5-R7",
        "scenarios": out,
    })


def gen_negative():
    pairs = [(0, 5), (10, 3), (7, 7), (20, 20), (30, 25)]
    cleaned, report = normalize_pairs(pairs, negative_policy="normalize")
    entry_normalize = {
        "policy": "normalize",
        "input": [[s, e] for s, e in pairs],
        "cleaned": [[s, e] for s, e in cleaned],
        "negative_report": report,
        "merged_touch_false": [[s, e] for s, e in merge_intervals(cleaned)],
        "merged_touch_true": [[s, e] for s, e in merge_intervals(cleaned, merge_touching=True)],
    }
    error = None
    try:
        normalize_pairs(pairs, negative_policy="strict")
    except ValueError as exc:
        error = str(exc)
    entry_strict = {
        "policy": "strict",
        "input": [[s, e] for s, e in pairs],
        "result": "raise ValueError",
        "error": error,
    }
    write("negative_results.json", {
        "description": "负长度区间（start > end）两种策略的处理结果；规则见 RULES.md R8",
        "cases": [entry_strict, entry_normalize],
    })


def gen_tz_precision_diff():
    raw_intervals = [
        ("2026-01-01T08:00:00+08:00", "2026-01-01T09:00:00+08:00"),
        ("2026-01-01T00:30:00Z", "2026-01-01T01:00:00Z"),
        ("2026-01-01T02:00:00", "2026-01-01T03:00:00"),          # naive -> 假定 UTC
        ("2026-01-01T03:00:00.123456789Z", "2026-01-01T04:00:00Z"),  # 纳秒 -> 截断
        (1767225600.123456789, 1767229200.5),                     # float epoch -> 截断
        ("2026-01-01T12:00:00+08:00", "2026-01-01T08:30:00+04:00"),
    ]
    normalized, reports = normalize_intervals(raw_intervals, negative_policy="normalize")
    entries = []
    for rep, (ns, ne) in zip(reports, normalized):
        entries.append({
            "index": rep.index,
            "input": {"start": str(rep.start.raw), "end": str(rep.end.raw)},
            "normalized": {"start": ns.isoformat(), "end": ne.isoformat()},
            "start_tz_assumed_utc": rep.start.tz_assumed,
            "end_tz_assumed_utc": rep.end.tz_assumed,
            "start_sub_micro_truncated": rep.start.sub_micro or None,
            "end_sub_micro_truncated": rep.end.sub_micro or None,
            "negative_action": rep.negative_action,
        })
    write("tz_precision_diff.json", {
        "description": "时区与精度统一前后差异：所有端点统一为 UTC、微秒精度；"
                       "亚微秒部分截断（T3），无时区输入假定 UTC（T2）",
        "entries": entries,
    })
    return raw_intervals


def gen_pre_post_merge_diff(raw_intervals):
    # 统一前（错误示范 A）：丢掉时区偏移，直接把各自墙钟当 UTC 合并（确定性算法，
    # 与运行环境时区无关）
    import re
    from datetime import datetime as dt_cls

    def wallclock_as_utc_micros(v):
        m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?", str(v))
        frac = (m.group(2) + "000000")[:6] if m.group(2) else "000000"
        d = dt_cls.fromisoformat(m.group(1) + "." + frac).replace(tzinfo=timezone.utc)
        return int(d.timestamp()) * 1_000_000 + int(frac)

    def nanos(v):
        # 按原始纳秒精度解析（不截断），用于“统一前”的精度对照
        m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?", str(v))
        frac = ((m.group(2) or "") + "000000000")[:9]
        d = dt_cls.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
        return int(d.timestamp()) * 1_000_000_000 + int(frac)

    case_a = [
        ("2026-01-01T08:00:00+08:00", "2026-01-01T09:00:00+08:00"),
        ("2026-01-01T00:30:00Z", "2026-01-01T01:00:00Z"),
    ]
    case_b = [
        ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00.1234567Z"),
        ("2026-01-01T00:00:00.1234569Z", "2026-01-01T01:00:00Z"),
    ]
    cases = []
    for name, intervals, before_fn, mt in [
        ("跨时区重叠（+08:00 与 UTC）", case_a, wallclock_as_utc_micros, False),
        ("亚微秒间隙（纳秒 0.2us 间隙，截断后相接）", case_b, nanos, True),
    ]:
        before_pairs = [(before_fn(s), before_fn(e)) for s, e in intervals]
        before = merge_intervals(before_pairs, merge_touching=mt)
        after = merge_datetime_intervals(intervals, negative_policy="normalize",
                                         merge_touching=mt)
        cases.append({
            "case": name,
            "input": [[str(s), str(e)] for s, e in intervals],
            "merge_touching": mt,
            "merge_before_unify_raw_axis": [[s, e] for s, e in before],
            "merge_after_unify_utc_iso": after["merged_iso"],
            "changed": [[s, e] for s, e in before] != after["merged_iso"],
        })
    write("pre_post_merge_diff.json", {
        "description": "同一批数据：统一前（时区场景=丢掉偏移拿墙钟当 UTC；精度场景=按原始"
                       "纳秒时间轴）与统一后（先转 UTC、截断到微秒再合并）的结果差异；"
                       "时区场景数值轴为微秒，精度场景数值轴为纳秒",
        "cases": cases,
    })


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    n = gen_crosscheck()
    gen_zero_length()
    gen_negative()
    raw = gen_tz_precision_diff()
    gen_pre_post_merge_diff(raw)
    print("全部数据文件生成完毕，对拍 %d 组全部一致。" % n)


if __name__ == "__main__":
    main()
