"""四种典型情形的诊断演示，并导出最小性验证数据（JSON）。

运行：python3 examples/demo.py
输出：终端打印四份报告；verification_data.json 写入仓库根目录。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conflict_locator import Constraint, diagnose, render_report


def C(u, v, c, cid=""):
    return Constraint(u, v, c, cid)


SCENARIOS = {
    "1_无冲突": [
        C(2, 1, 5, "req1"),   # x2 - x1 <= 5
        C(3, 2, 2, "req2"),   # x3 - x2 <= 2
        C(1, 3, 0, "req3"),   # x1 - x3 <= 0
    ],
    "2_单一最小冲突": [
        C(1, 2, -1, "deadline"),   # x1 - x2 <= -1
        C(2, 1, 0, "order"),       # x2 - x1 <= 0 -> 与上一条矛盾
        C(3, 1, 5, "unrelated"),   # 无关约束，不应进入冲突集
    ],
    "3_多个最小冲突": [
        C(1, 2, -1, "a1"), C(2, 1, 0, "a2"),      # 冲突环 A
        C(3, 4, -1, "b1"), C(4, 3, 0, "b2"),      # 冲突环 B
        C(1, 3, 7, "bridge"),                      # 跨组约束，可满足
    ],
    "4_约束自相矛盾": [
        C(2, 1, 3, "ok1"),
        C(1, 1, -1, "bad"),   # x1 - x1 <= -1，单条即不可满足
    ],
}


def main():
    export = {}
    for name, cons in SCENARIOS.items():
        diag = diagnose(cons, max_mus=20, max_calls=500)
        print(f"\n### 情形 {name}")
        print(render_report(diag))
        export[name] = {
            "status": diag.status,
            "n_constraints": diag.n_constraints,
            "enumeration_complete": diag.enumeration_complete,
            "self_contradictory": diag.self_contradictory_cids,
            "conflicts": [
                {
                    "cids": list(mc.cids),
                    "size": mc.size,
                    "cycle_weight": mc.cycle_weight,
                    "verified_minimal": mc.verified_minimal,
                    "deletion_checks": [
                        {"removed": cid, "remainder_satisfiable": ok}
                        for cid, ok in mc.verification
                    ],
                }
                for mc in diag.conflicts
            ],
        }
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "verification_data.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    print(f"\n最小性验证数据已写入 {out}")


if __name__ == "__main__":
    main()
