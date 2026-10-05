"""运行全部手工对拍用例，输出环报告 cycle_report.json（含对拍结论）。"""

import json

from recquery import build_graph, query
from cases import QUERY_CASES


def main():
    report = []
    all_match = True
    for case in QUERY_CASES:
        graph, dups = build_graph(case["edges"])
        result = query(graph, case["start"], case["max_depth"])
        actual = {
            "status": result["status"],
            "visited": result["visited"],
            "truncated": result["truncated"],
            "cycle_nodes": [r["nodes"] for r in result["cycles"]],
            "cycle_sequences": [s for r in result["cycles"] for s in r["cycles"]],
        }
        expected = {
            "status": case["expected_status"],
            "visited": case["expected_visited"],
            "truncated": case["expected_truncated"],
            "cycle_nodes": case["expected_cycle_nodes"],
            "cycle_sequences": case["expected_cycle_sequences"],
        }
        # JSON 化后比较（键统一为字符串）
        match = json.loads(json.dumps(actual)) == json.loads(json.dumps(expected))
        all_match &= match
        report.append({
            "name": case["name"],
            "desc": case["desc"],
            "duplicates_removed": dups,
            "expected": expected,
            "actual": actual,
            "match": match,
        })
    summary = {"cases": len(report), "all_match": all_match, "report": report}
    with open("cycle_report.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"cases={len(report)} all_match={all_match} -> cycle_report.json")
    return 0 if all_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
