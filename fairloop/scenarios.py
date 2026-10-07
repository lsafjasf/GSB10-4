"""四类标准场景：短任务 / 单个超长任务 / 周期中等任务 / 大量空闲时间片。

所有场景只依赖虚拟时钟，时间参数显式注入，产出完全确定。
"""

from .loop import FairLoop, VirtualClock
from .monitor import FairnessMonitor


def scenario_short_tasks(n=1000, tick=0.001):
    """全为短任务：每个回调占用 tick 秒。"""
    clock = VirtualClock()
    monitor = FairnessMonitor()
    loop = FairLoop(clock, monitor)
    for i in range(n):
        loop.schedule(lambda: tick, name=f"short#{i}", site="scenario:short")
    loop.drain()
    return monitor.records


def scenario_one_hog(n_short=100, hog=5.0, tick=0.001):
    """单个超长任务：一个 hog 回调把后续短任务全部拖住。"""
    clock = VirtualClock()
    monitor = FairnessMonitor()
    loop = FairLoop(clock, monitor)
    loop.schedule(lambda: hog, name="hog", site="scenario:hog")
    for i in range(n_short):
        loop.schedule(lambda: tick, name=f"queued#{i}", site="scenario:queued")
    loop.drain()
    return monitor.records


def scenario_periodic_medium(periods=20, cost=0.05, tick=0.001, per_period=5):
    """周期性中等任务：medium 任务每期重注册自己，并伴随若干短任务。"""
    clock = VirtualClock()
    monitor = FairnessMonitor()
    loop = FairLoop(clock, monitor)

    def medium():
        if medium.left > 1:
            medium.left -= 1
            loop.schedule(medium, name="periodic-medium", site="scenario:periodic")
        return medium.cost

    medium.left = periods
    medium.cost = cost
    loop.schedule(medium, name="periodic-medium", site="scenario:periodic")
    for p in range(periods):
        for i in range(per_period):
            loop.schedule(lambda: tick, name=f"tick#{p}:{i}", site="scenario:tick")
        # 每期执行 1 个 medium + per_period 个短任务；
        # medium 在执行中重注册自己，留给下一期。
        loop.run(1 + per_period)
    loop.drain()
    return monitor.records


def scenario_idle_slices(n=50, tick=0.001, idle=0.1):
    """大量空闲时间片：回调之间显式 advance 出空闲间隔。"""
    clock = VirtualClock()
    monitor = FairnessMonitor()
    loop = FairLoop(clock, monitor)
    for i in range(n):
        loop.schedule(lambda: tick, name=f"sparse#{i}", site="scenario:sparse")
        loop.run(1)
        clock.advance(idle)
    return monitor.records
