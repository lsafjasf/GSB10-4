"""对拍校验：读取 data/workload.jsonl，在第 120 个操作后模拟进程重启（关闭并重新打开队列，
仅从 WAL 恢复），输出 data/restart_trace.jsonl 并与 data/golden_trace.jsonl 逐字节比对。

用法: python3 replay_check.py   # 退出码 0 = 恢复后行为与未重启完全一致
"""
import json
import os
import sys

from gen_golden import DATA, GOLDEN, RESTART_AT_OP, WORKLOAD, run


def main():
    with open(WORKLOAD, encoding="utf-8") as f:
        ops = [tuple(json.loads(line)) for line in f]
    trace = run(ops, os.path.join(DATA, "restart.log"), restart_at=RESTART_AT_OP)
    restart_path = os.path.join(DATA, "restart_trace.jsonl")
    with open(restart_path, "w", encoding="utf-8") as f:
        for rec in trace:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    with open(GOLDEN, encoding="utf-8") as f:
        golden = [json.loads(line) for line in f]
    # 重启轨迹会多一条 restart 事件，剔除后必须与 golden 完全一致
    stripped = [r for r in trace if r["event"] != "restart"]
    if stripped == golden:
        print(f"OK: 重启恢复后的 {len(stripped)} 条轨迹与未重启基准完全一致")
        print(f"restart trace -> {restart_path}")
        return 0
    for i, (a, b) in enumerate(zip(golden, stripped)):
        if a != b:
            print(f"MISMATCH at trace line {i}:\n  golden : {a}\n  restart: {b}")
            break
    else:
        print(f"LENGTH MISMATCH: golden={len(golden)} restart={len(stripped)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
