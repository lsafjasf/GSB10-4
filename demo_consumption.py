"""预算消耗序列演示：python3 demo_consumption.py

每个场景打印逐事件消耗序列（虚拟毫秒），并汇总写入 data/consumption_sequences.json。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from budget_chain import FakeClock, Stage, TimeBudget, run_chain


def stage(name, plan, **kw):
    calls = {"n": 0}

    def handler(ctx, grant):
        clock = handler.clock
        action = plan[min(calls["n"], len(plan) - 1)]
        calls["n"] += 1
        if action == "timeout" or action > grant:
            clock.sleep(grant)
            raise TimeoutError(f"{name} downstream timeout after {grant}ms")
        clock.sleep(action)
        return f"{name}:ok#{calls['n']}"

    return Stage(name=name, handler=handler, **kw)


def run(name, total_ms, stages, queue=0.0, note=""):
    clock = FakeClock(0)
    for s in stages:
        s.handler.clock = clock
    budget = TimeBudget.start(total_ms, clock=clock, sleep=clock.sleep)
    result = run_chain(budget, stages, queue_wait_ms=queue)
    record = {
        "scenario": name,
        "note": note,
        "budget_ms": total_ms,
        "status": result.status,
        "reason": result.reason,
        "elapsed_ms": round(result.elapsed_ms, 4),
        "remaining_ms": round(result.remaining_ms, 4),
        "attempts_by_stage": result.attempts_by_stage,
        "events": result.events,
    }
    print(f"\n=== {name} === {note}")
    print(f"status={result.status} reason={result.reason!r} "
          f"elapsed={result.elapsed_ms:.2f}ms remaining={result.remaining_ms:.2f}ms "
          f"attempts={result.attempts_by_stage}")
    for e in result.events:
        detail = {k: v for k, v in e.items() if k not in ("kind", "at_ms")}
        print(f"  t={e['at_ms']:>8.2f}ms  {e['kind']:<18} {detail}")
    return record


def main():
    records = []

    # 1) 重试耗尽：单段 3 次尝试，退避 5->10，硬上限 35ms
    records.append(run(
        "retry_exhaustion",
        100,
        [stage("S", ["timeout"], max_attempts=3, max_attempt_ms=35,
               backoff_base_ms=5, backoff_factor=2)],
        note="3 次尝试共享同一份 100ms 预算；朴素方案(每次独立 35ms)会用 35+5+35+10+35=120ms",
    ))
    naive_total = 35 + 5 + 35 + 10 + 35
    print(f"  -> 朴素重试总时长 {naive_total}ms = 预算的 {naive_total/100:.2f}x；"
          f"本库 {records[-1]['elapsed_ms']:.2f}ms <= 100ms，重试不会放大总时长")
    records[-1]["naive_total_ms"] = naive_total

    # 2) 重试后成功
    records.append(run(
        "retry_then_success",
        100,
        [stage("S", ["timeout", 10], max_attempts=2, backoff_base_ms=5)],
        note="第 1 次超时(50ms 切片)，退避 5ms，第 2 次 10ms 成功",
    ))

    # 3) 全部成功（含排队）
    records.append(run(
        "all_success_with_queue",
        120,
        [stage("A", [20]), stage("B", [25]), stage("C", [30])],
        queue=15,
        note="排队 15ms 计入预算；三段等权各分 40ms 切片，未用完的留给后段",
    ))

    # 4) 首段超时：后续段不调度
    records.append(run(
        "first_stage_timeout",
        100,
        [stage("A", ["timeout"], max_attempt_ms=20),
         stage("B", [1]), stage("C", [1])],
        note="首段挂死，授权被硬上限压到 20ms；显式 stage_timeout，B/C 不进入",
    ))

    # 5) 末段超时（预算仍有剩余）
    records.append(run(
        "last_stage_timeout",
        100,
        [stage("A", [30]), stage("B", [30]),
         stage("C", ["timeout"], max_attempt_ms=25)],
        note="末段挂死其 25ms 硬上限；显式 stage_timeout，剩余 15ms 如实上报",
    ))

    # 6) 排队即耗尽
    records.append(run(
        "queue_starvation",
        100,
        [stage("A", [1])],
        queue=100,
        note="排队 100ms 吃光预算；首段开始前即显式 exhausted，不发起任何调用",
    ))

    # 7) 时钟回拨
    clock = FakeClock(0)

    def advance_a(ctx, grant):
        clock.set(40)  # 预算在本段结束时观察到高水位 40
        return "A:ok"

    def rollback_b(ctx, grant):
        clock.set(10)  # 时钟回拨 30ms
        clock.sleep(30)  # 回拨窗口内工作 30ms：裸时钟回到 40，包络水位不变
        return "B:ok"

    budget = TimeBudget.start(100, clock=clock, sleep=clock.sleep)
    result = run_chain(budget, [Stage("A", advance_a), Stage("B", rollback_b)])
    rec = {
        "scenario": "clock_rollback",
        "note": "A 后时钟在 B 内从 40 回拨到 10，B 工作 30ms；包络口径下回拨窗口不计，"
                "elapsed=40，预算不会被放大",
        "budget_ms": 100,
        "status": result.status,
        "reason": result.reason,
        "elapsed_ms": round(result.elapsed_ms, 4),
        "remaining_ms": round(result.remaining_ms, 4),
        "attempts_by_stage": result.attempts_by_stage,
        "events": result.events,
    }
    records.append(rec)
    print(f"\n=== clock_rollback === {rec['note']}")
    print(f"status={result.status} elapsed={result.elapsed_ms:.2f}ms "
          f"remaining={result.remaining_ms:.2f}ms (若按裸时钟会被回拨放大成 90ms)")
    for e in result.events:
        detail = {k: v for k, v in e.items() if k not in ("kind", "at_ms")}
        print(f"  t={e['at_ms']:>8.2f}ms  {e['kind']:<18} {detail}")

    out = os.path.join(os.path.dirname(__file__), "data", "consumption_sequences.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"\n已写入 {out}")


if __name__ == "__main__":
    main()
