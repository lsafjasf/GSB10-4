"""生成对拍基准：同一份确定性操作脚本，不重启跑一遍，输出投递轨迹到 data/golden_trace.jsonl。

用法: python3 gen_golden.py
"""
import json
import os

from delayq import DelayQueue

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
WORKLOAD = os.path.join(DATA, "workload.jsonl")
GOLDEN = os.path.join(DATA, "golden_trace.jsonl")
RESTART_AT_OP = 120  # replay_check.py 在第 120 个操作后模拟重启


def build_ops():
    """确定性操作脚本（无随机数）：覆盖立即到期/远期/同刻堆积/各类取消。"""
    ops = []
    # 1) 立即到期与过去时间
    for i in range(10):
        ops.append(("S", f"imm{i}", -i, f"payload-imm{i}"))
    ops.append(("SCAN", 0))
    # 2) 同一时刻大堆积（验证同刻按入队序号）
    for i in range(60):
        ops.append(("S", f"same{i}", 5000, f"p{i}"))
    # 3) 不同到期时间交错入队
    for i in range(40):
        ops.append(("S", f"mix{i}", (i % 7) * 1000 + 1000, f"m{i}"))
    # 4) 远期消息（重启前不会到期）
    for i in range(20):
        ops.append(("S", f"far{i}", 10**9 + i, f"f{i}"))
    # 5) 各类取消：未到期 / 已投递 / 不存在 / 重复取消
    ops.append(("C", "imm3"))        # 已投递 -> delivered
    ops.append(("C", "imm3"))        # 重复 -> delivered（幂等）
    ops.append(("C", "same7"))       # 未到期 -> ok
    ops.append(("C", "same7"))       # 重复 -> canceled
    ops.append(("C", "ghost"))       # 不存在 -> unknown
    ops.append(("C", "mix0"))        # 未到期 -> ok
    ops.append(("C", "far5"))        # 远期取消 -> ok
    # 6) 第一次大扫描（same* 与部分 mix* 到期）
    ops.append(("SCAN", 5000))
    # 7) 到期未扫描前取消（应成功）与扫描后取消（应 delivered）
    ops.append(("S", "late1", 6000, "late"))
    ops.append(("C", "late1"))       # 6000 时刻还没到，未到期 -> ok
    ops.append(("S", "late2", 6000, "late"))
    ops.append(("SCAN", 6000))       # late2 在此投递
    ops.append(("C", "late2"))       # 已投递 -> delivered
    # 8) 重启点之后：新消息与旧消息同刻到期，验证 seq 延续
    for i in range(15):
        ops.append(("S", f"post{i}", 5000, f"post{i}"))
    ops.append(("SCAN", 7000))
    # 9) 远期消息到期
    ops.append(("SCAN", 10**9 + 100))
    # 10) 重启后再取消已投递/已取消，验证状态持久
    ops.append(("C", "post0"))       # 已投递 -> delivered
    ops.append(("C", "same8"))       # 已投递 -> delivered
    ops.append(("C", "same7"))       # 已取消 -> canceled
    return ops


def run(ops, path, restart_at=None):
    if os.path.exists(path):
        os.remove(path)
    q = DelayQueue(path)
    trace = []
    for idx, op in enumerate(ops):
        if restart_at is not None and idx == restart_at:
            q.close()
            q = DelayQueue(path)  # 模拟进程重启：仅从 WAL 恢复
            trace.append({"event": "restart", "recovered": q.recovered})
        kind = op[0]
        if kind == "S":
            _, mid, at, payload = op
            seq = q.schedule(mid, at, payload)
            trace.append({"event": "schedule", "id": mid, "at": at, "seq": seq})
        elif kind == "C":
            st = q.cancel(op[1])
            trace.append({"event": "cancel", "id": op[1], "status": st})
        else:
            got = q.due(op[1])
            trace.append({
                "event": "scan", "now": op[1],
                "delivered": [[d.id, d.at_ms, d.seq] for d in got],
            })
    q.close()
    return trace


def main():
    os.makedirs(DATA, exist_ok=True)
    ops = build_ops()
    with open(WORKLOAD, "w", encoding="utf-8") as f:
        for op in ops:
            f.write(json.dumps(op) + "\n")
    trace = run(ops, os.path.join(DATA, "golden.log"))
    with open(GOLDEN, "w", encoding="utf-8") as f:
        for rec in trace:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    scans = [r for r in trace if r["event"] == "scan"]
    total = sum(len(s["delivered"]) for s in scans)
    print(f"ops={len(ops)} scans={len(scans)} delivered_total={total}")
    print(f"workload -> {WORKLOAD}")
    print(f"golden   -> {GOLDEN}")


if __name__ == "__main__":
    main()
