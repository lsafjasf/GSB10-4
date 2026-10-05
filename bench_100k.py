"""10 万次事务压测：重试次数分布、终态分布、调度耗时、预算消耗。

时间由 FakeClock 注入：退避只推进虚拟时钟，不真睡，
因此"虚拟调度时长"（事务真正要等多久）与"墙钟派发耗时"
（调度器跑 10 万次循环本身多贵）分开统计。

运行：python3 bench_100k.py
"""

import random
import time
from collections import Counter

from txretry import (
    AbortError,
    DeadlockError,
    FatalTransactionError,
    FakeClock,
    RetryBudgetExhausted,
    RetryClass,
    RetryPolicy,
    RetryState,
    SerializationError,
    classify,
    run_transaction,
)

N_TRANSACTIONS = 100_000
SEED = 20240101

# 10 万事务的工作负载模型（固定种子，完全可复现）：
#  - 88% 健康事务：每次尝试以 p_conflict 概率遭遇冲突，几何分布
#  - 3%  持久冲突：持续冲突直到预算/次数耗尽（用于观察终态）
#  - 5%  不可重试：首次尝试就抛约束/业务错误
#  - 4%  先冲突后致命：重试 1~2 次后遇到唯一键冲突
POLICY = RetryPolicy(max_attempts=8, base_delay=0.010, max_delay=1.0,
                     time_budget=5.0, jitter="full")
P_CONFLICT = 0.40
CONFLICTS = (DeadlockError, SerializationError)


class UniqueViolation(FatalTransactionError):
    def __init__(self):
        super().__init__("duplicate key value violates unique constraint")
        self.sqlstate = "23505"


def build_workload(rng):
    for i in range(N_TRANSACTIONS):
        roll = rng.random()
        if roll < 0.88:
            kind = "healthy"
        elif roll < 0.91:
            kind = "persistent"
        elif roll < 0.96:
            kind = "fatal"
        else:
            kind = "conflict_then_fatal"

        conflict_exc = CONFLICTS[rng.randrange(len(CONFLICTS))]
        fatal_after = rng.randint(1, 2)

        def make_tx(kind, conflict_exc, fatal_after):
            state = {"attempts": 0}

            def tx():
                state["attempts"] += 1
                if kind == "fatal":
                    raise AbortError("余额不足")
                if kind == "persistent":
                    raise conflict_exc(f"victim tx")
                if kind == "conflict_then_fatal":
                    if state["attempts"] <= fatal_after:
                        raise conflict_exc()
                    raise UniqueViolation()
                # healthy：几何分布冲突
                if rng.random() < P_CONFLICT:
                    raise conflict_exc()
                return "committed"

            return tx
        yield make_tx(kind, conflict_exc, fatal_after)


def main():
    rng = random.Random(SEED)
    retry_hist: Counter[int] = Counter()          # 成功事务的重试次数
    terminal_attempts: Counter[int] = Counter()   # 失败事务的最终尝试次数
    terminal_hist: Counter[str] = Counter()
    total_virtual_backoff = 0.0
    total_virtual_elapsed = 0.0
    max_single_backoff = 0.0

    wall_start = time.perf_counter()
    for tx in build_workload(rng):
        clock = FakeClock()
        tx_rng = random.Random(rng.getrandbits(64))
        try:
            result = run_transaction(tx, policy=POLICY, clock=clock,
                                     rng=tx_rng)
        except RetryBudgetExhausted as terminal:
            terminal_hist[terminal.state.value] += 1
            terminal_attempts[terminal.attempts] += 1
            total_virtual_elapsed += terminal.elapsed
        except FatalTransactionError:
            terminal_hist["fatal"] += 1
        else:
            terminal_hist["success"] += 1
            retry_hist[result.retries] += 1
            total_virtual_elapsed += result.elapsed
        total_virtual_backoff += sum(clock.sleeps)
        if clock.sleeps:
            max_single_backoff = max(max_single_backoff, max(clock.sleeps))
    wall_elapsed = time.perf_counter() - wall_start

    n = N_TRANSACTIONS
    print("=" * 68)
    print(f"10 万次事务压测（种子 {SEED}，full jitter）")
    print("=" * 68)

    print("\n[终态分布]")
    for state, count in sorted(terminal_hist.items()):
        print(f"  {state:<18} {count:>7,}  {count / n:7.2%}")

    print("\n[成功事务的重试次数分布]")
    success_total = terminal_hist["success"]
    for k in range(0, POLICY.max_attempts):
        c = retry_hist.get(k, 0)
        bar = "#" * int(40 * c / max(retry_hist.values()))
        print(f"  {k} 次重试: {c:>7,}  ({c / success_total:6.2%}) {bar}")
    print("\n[失败事务的最终尝试次数分布]")
    for k in sorted(terminal_attempts):
        print(f"  {k} 次尝试后放弃: {terminal_attempts[k]:>7,}")
    mean_retries = sum(k * v for k, v in retry_hist.items()) / success_total
    percentiles = {}
    cumulative = 0
    for k in range(0, POLICY.max_attempts):
        cumulative += retry_hist.get(k, 0)
        for q in (0.5, 0.9, 0.99):
            if q not in percentiles and cumulative / success_total >= q:
                percentiles[q] = k
    print(f"  成功事务平均重试次数: {mean_retries:.4f}")
    print(f"  成功事务 P50/P90/P99 重试次数: "
          f"{percentiles[0.5]}/{percentiles[0.9]}/{percentiles[0.99]}")

    print("\n[调度耗时]")
    print(f"  墙钟派发总耗时（调度器循环，真实 sleep 为 0）: {wall_elapsed:.3f} s")
    print(f"  每事务墙钟调度开销: {wall_elapsed / n * 1e6:.2f} µs")
    print(f"  退避累计虚拟时长（串行重放需等待）: {total_virtual_backoff:,.2f} s")
    print(f"  每事务平均虚拟退避: {total_virtual_backoff / n * 1000:.3f} ms")
    print(f"  事务全程虚拟时长累计（含执行模型为 0 的尝试）: "
          f"{total_virtual_elapsed:,.2f} s")
    print(f"  观测到的最大单次退避: {max_single_backoff:.6f} s "
          f"(max_delay={POLICY.max_delay})")

    # 单事务样例：固定策略 + 固定种子的重试时刻序列
    print("\n[单事务重试时刻序列（seed=42，base=10ms，cap=1s，full jitter）]")
    demo_policy = RetryPolicy(max_attempts=8, base_delay=0.010,
                              max_delay=1.0, time_budget=5.0, jitter="full")
    clock = FakeClock()
    fail = {"n": 0}

    def always_deadlock():
        fail["n"] += 1
        raise DeadlockError("deadlock detected")

    timeline = []
    try:
        run_transaction(
            always_deadlock, policy=demo_policy, clock=clock,
            rng=random.Random(42),
            on_retry=lambda r: timeline.append(
                (r.attempt, r.started_at, r.delay_after)))
    except RetryBudgetExhausted:
        pass
    print(f"  {'尝试':>4} {'失败时刻(s)':>12} {'退避(s)':>12} {'下次时刻(s)':>14}")
    for attempt, started_at, delay in timeline:
        if delay > 0:
            print(f"  {attempt:>4} {started_at:>12.6f} {delay:>12.6f} "
                  f"{started_at + delay:>14.6f}")
        else:
            print(f"  {attempt:>4} {started_at:>12.6f} {'--':>12} "
                  f"{'最后一次，不再退避':>14}")
    print(f"  第 {len(timeline)} 次（最后一次）尝试仍失败 -> 终态 EXHAUSTED，"
          f"保留 last_error='deadlock detected'")

    # 预算消耗演示：每次尝试执行耗时 0.3s，持续死锁，预算 2.0s
    print("\n[共享预算消耗演示（每次执行 0.3s，持续死锁，预算 2.0s）]")
    bp = RetryPolicy(max_attempts=8, base_delay=0.01, max_delay=1.0,
                     time_budget=2.0, jitter="full")
    bclock = FakeClock()

    def costly_deadlock():
        bclock.advance(0.3)
        raise DeadlockError("deadlock detected")

    rows = []
    try:
        run_transaction(
            costly_deadlock, policy=bp, clock=bclock,
            rng=random.Random(7),
            on_retry=lambda r: rows.append(
                (r.attempt, r.started_at, r.delay_after)))
    except RetryBudgetExhausted as terminal:
        print(f"  {'尝试':>4} {'开始(s)':>10} {'退避(s)':>10} "
              f"{'已消耗(s)':>12} {'剩余预算(s)':>12}")
        for attempt, started_at, delay in rows:
            consumed = started_at + 0.3 + delay
            print(f"  {attempt:>4} {started_at:>10.3f} {delay:>10.3f} "
                  f"{consumed:>12.3f} {bp.time_budget - consumed:>12.3f}")
        print(f"  -> 终态 {terminal.state.value}，共 {terminal.attempts} 次尝试，"
              f"elapsed={terminal.elapsed:.3f}s")
        print(f"  -> 预算 2.0s 共享于全部轮次：调度（是否再试+睡多久）"
              f"从不越过预算，总时长上界 = 预算 + 单次执行耗时")

    # 抖动防同步对照：100 个并发受害者同一时刻失败，无抖动 vs full jitter
    print("\n[防同步对照：100 个客户端同时遭遇死锁后的第二次重试时刻]")
    p_none = RetryPolicy(base_delay=0.01, max_delay=1.0, jitter="none")
    p_full = RetryPolicy(base_delay=0.01, max_delay=1.0, jitter="full")
    none_times, full_times = [], []
    for i in range(100):
        none_times.append(p_none.compute_delay(1, random.Random(1000 + i)))
        full_times.append(p_full.compute_delay(1, random.Random(1000 + i)))
    spread_none = max(none_times) - min(none_times)
    spread_full = max(full_times) - min(full_times)
    print(f"  无抖动: 所有时刻={none_times[0]:.4f}s，跨度={spread_none:.6f}s "
          f"-> 100 个客户端在同一时刻撞回")
    print(f"  full jitter: 时刻分布于 [{min(full_times):.4f}, "
          f"{max(full_times):.4f}]s，跨度={spread_full:.4f}s -> 摊开")


if __name__ == "__main__":
    main()
