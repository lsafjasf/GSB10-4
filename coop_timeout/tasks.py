"""示例协作式任务：演示检查点放置与资源清理。

时间线（虚拟时间，单位任意）：

    t=0     入口检查点（阶段 INIT）
    t=1     资源 A 获取完成（cost=1），过检查点（阶段 ACQUIRE_A）
    t=3     资源 B 获取完成（cost=2），过检查点（阶段 ACQUIRE_B）
    t=3..7  工作循环 4 步：每步前过检查点（t=3,4,5,6），每步推进 1（阶段 WORK）
    t=7     提交结果，运行器过提交边界检查点（阶段 SUBMIT）
    t=7..9  清理：释放 B（t=7 开始，耗时 1）、释放 A（t=8 开始，耗时 1），
            不可取消区

资源台账 ``ResourceLedger`` 记录每次 acquire/release，测试据此验证
「中断点落在任何阶段，资源都不泄漏」。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .runtime import TaskContext


class ResourceLedger:
    """进程内资源台账：记录获取/释放流水，并提供泄漏查询。"""

    def __init__(self) -> None:
        self.events: List[Tuple[str, str, float]] = []  # (action, name, t)

    def acquire(self, name: str, t: float) -> None:
        self.events.append(("acquire", name, t))

    def release(self, name: str, t: float) -> None:
        self.events.append(("release", name, t))

    def held(self) -> List[str]:
        held: List[str] = []
        for action, name, _t in self.events:
            if action == "acquire":
                held.append(name)
            else:
                held.remove(name)
        return held

    def releases(self) -> List[str]:
        return [name for action, name, _t in self.events if action == "release"]


def make_resource_task(ledger: ResourceLedger):
    """构造一个获取 A、B 两个资源并工作 4 步的协作式任务。"""

    def task(ctx: TaskContext) -> Dict[str, object]:
        def release_a() -> None:
            ledger.release("A", ctx.clock.now)
            ctx.clock.advance(1.0)  # 释放本身耗时，且不可被中断

        def release_b() -> None:
            ledger.release("B", ctx.clock.now)
            ctx.clock.advance(1.0)

        ctx.enter_phase("ACQUIRE_A")
        ctx.acquire(
            "A",
            release=release_a,
            cost=1.0,
            on_acquire=lambda: ledger.acquire("A", ctx.clock.now),
        )

        ctx.enter_phase("ACQUIRE_B")
        ctx.acquire(
            "B",
            release=release_b,
            cost=2.0,
            on_acquire=lambda: ledger.acquire("B", ctx.clock.now),
        )

        ctx.enter_phase("WORK")
        ctx.work(4.0, steps=4)  # 每步 1.0，步间是检查点

        ctx.enter_phase("SUBMIT")
        return ctx.complete({"result": "done", "steps": 4})

    return task


def make_failing_task(ledger: ResourceLedger):
    """构造一个获取资源后在工作中途自身失败的任务（与中断无关）。"""

    def task(ctx: TaskContext) -> Dict[str, object]:
        def release_a() -> None:
            ledger.release("A", ctx.clock.now)
            ctx.clock.advance(1.0)

        ctx.enter_phase("ACQUIRE_A")
        ctx.acquire(
            "A",
            release=release_a,
            cost=1.0,
            on_acquire=lambda: ledger.acquire("A", ctx.clock.now),
        )

        ctx.enter_phase("WORK")
        ctx.work(1.0)
        raise RuntimeError("boom: task's own bug")

    return task


def make_no_checkpoint_task():
    """反例：长工作段不放检查点。用于证明运行时绝不强制抢占——
    超时信号只能等到任务返回后的提交边界检查点才被响应。"""

    def task(ctx: TaskContext) -> str:
        ctx.enter_phase("WORK")
        ctx.clock.advance(10.0)  # 10 个时间单位内没有任何检查点
        ctx.enter_phase("SUBMIT")
        return ctx.complete("finished-without-checkpoints")

    return task
