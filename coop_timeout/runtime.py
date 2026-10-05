"""协作式超时/取消运行时（仅标准库，时间由测试注入）。

核心约定
========
1. 中断是协作式的：运行时只在「安全检查点」响应中断信号，绝不在任意位置
   强制打断任务代码。任务代码在阶段边界、每个工作步之前、资源获取之后
   主动调用 ``ctx.checkpoint()``。
2. 清理逻辑（资源释放）在不可取消区内执行，期间到达的中断信号只被记录
   （``deferred``），绝不打断清理本身。
3. 结果可观测：``Outcome.kind`` 区分 OK / TIMEOUT / CANCELLED / FAILED，
   并携带中断点、延迟、已释放资源等字段。
4. 任务一旦提交结果（complete），之后到达的信号一律记为 ``late``，
   不改变已提交的结果。

时间模型
========
``VirtualClock`` 是注入的虚拟时钟：任务代码通过 ``ctx.work()`` /
``ctx.acquire()`` 显式推进时间，外部事件（超时闹钟、取消信号）通过
``clock.schedule_at`` 排入事件堆，由 ``checkpoint()`` 在检查点泵出。
因此「何时超时、何时取消」完全由测试注入，与真实墙钟无关。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional, Tuple


class Kind(Enum):
    """任务终态。四种状态互斥，调用方可据此区分超时/主动取消/自身失败。"""

    OK = "OK"                # 正常完成
    TIMEOUT = "TIMEOUT"      # 到达 deadline，在检查点被协作式中断
    CANCELLED = "CANCELLED"  # 收到主动取消信号，在检查点被协作式中断
    FAILED = "FAILED"        # 任务自身抛出异常（与中断无关）


class VirtualClock:
    """注入式虚拟时钟 + 确定性事件调度器。

    - ``advance(dt)`` 只能由任务代码（经 TaskContext）调用，表示消耗时间。
    - ``schedule_at(t, fn)`` 把外部事件排在时刻 t；同一时刻的事件按注册
      顺序触发（FIFO），保证可复现。
    - ``pump()`` 触发所有已到期事件；只在检查点被调用。
    """

    def __init__(self) -> None:
        self.now: float = 0.0
        self._heap: List[Tuple[float, int, Callable[[], None]]] = []
        self._seq: int = 0
        self.events_fired: int = 0

    def advance(self, dt: float) -> None:
        if dt < 0:
            raise ValueError("dt must be >= 0")
        self.now += dt

    def schedule_at(self, t: float, fn: Callable[[], None]) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (t, self._seq, fn))

    def pump(self) -> int:
        fired = 0
        while self._heap and self._heap[0][0] <= self.now:
            _, _, fn = heapq.heappop(self._heap)
            fn()
            fired += 1
        self.events_fired += fired
        return fired


class CancellationToken:
    """中断信号源。超时与主动取消共用一条信号通道，但分别记录原因。"""

    def __init__(self, clock: VirtualClock, deadline: Optional[float] = None) -> None:
        self.clock = clock
        self.deadline = deadline
        self.timed_out = False
        self.cancel_requested = False
        self.latched_at: Optional[float] = None   # 首个信号被事件泵锁存的时间
        self.due_at: Optional[float] = None       # 首个信号的到期时间（可能更早）
        self.deferred: int = 0                    # 清理期间到达、被推迟的信号数
        self.late: int = 0                        # 任务完成后才到达的信号数
        self._listeners: List[Callable[[], None]] = []
        if deadline is not None:
            clock.schedule_at(deadline, lambda due=deadline: self._latch(True, False, due))

    def _latch(self, timed_out: bool, cancelled: bool, due_at: float) -> None:
        """锁存一个已到期的中断信号。``due_at`` 是信号本身到期的时刻，
        可能早于当前检查点（泵事件）的时刻——两者之差就是响应延迟。"""
        if timed_out:
            self.timed_out = True
        if cancelled:
            self.cancel_requested = True
        if self.latched_at is None:
            self.latched_at = self.clock.now
            self.due_at = due_at
        for listener in list(self._listeners):
            listener()

    def cancel(self, at: Optional[float] = None) -> None:
        """请求主动取消。``at=None`` 表示立即到期，否则在虚拟时刻 ``at`` 到期。"""
        if at is None or at <= self.clock.now:
            self._latch(False, True, self.clock.now)
        else:
            self.clock.schedule_at(at, lambda due=at: self._latch(False, True, due))

    def pending_reason(self) -> Optional[Kind]:
        """返回当前待响应的中断原因；同时挂起时主动取消优先（策略固定）。"""
        if self.cancel_requested:
            return Kind.CANCELLED
        if self.timed_out:
            return Kind.TIMEOUT
        return None


class Interrupt(BaseException):
    """在检查点抛出的协作式中断。继承 BaseException，避免被任务里
    粗放的 ``except Exception`` 吞掉。"""

    def __init__(self, reason: Kind, phase: str, at: float) -> None:
        super().__init__(f"{reason.value} at checkpoint in phase {phase!r} (t={at})")
        self.reason = reason
        self.phase = phase
        self.at = at


@dataclass
class _Lease:
    name: str
    release: Callable[[], None]
    acquired_at: float


@dataclass(frozen=True)
class Outcome:
    """一次运行的可观测结果（不可变）。

    - ``kind``：终态（OK/TIMEOUT/CANCELLED/FAILED）。
    - ``phase``：中断（或失败、完成）发生时所处的阶段。
    - ``latency``：从信号到期（``signal_at``）到检查点实际响应
      （``responded_at``）的虚拟时间间隔；仅 TIMEOUT/CANCELLED 时有意义。
      它上界 = 最长检查点间距 + 当时所处的不可取消区长度，可证明有界。
    - ``released``：清理阶段实际释放的资源（按获取逆序）。
    - ``deferred``：清理期间到达、被有意忽略（只记录）的中断信号数。
    - ``late``：任务提交结果之后才到达的信号数（不改变结果）。
    """

    kind: Kind
    value: Any = None
    error: Optional[str] = None
    phase: Optional[str] = None
    signal_at: Optional[float] = None  # 仅作“是否发生中断”哨兵
    responded_at: Optional[float] = None
    latency: Optional[float] = None
    released: Tuple[str, ...] = ()
    deferred: int = 0
    late: int = 0

    def fingerprint(self) -> Tuple[Any, ...]:
        """稳定指纹，用于「已完成再中断不改变结果」的对拍。"""
        return (
            self.kind,
            self.value,
            self.error,
            self.phase,
            self.signal_at,
            self.responded_at,
            self.latency,
            self.released,
            self.deferred,
            self.late,
        )


class TaskContext:
    """任务代码看到的协作式上下文。"""

    def __init__(self, clock: VirtualClock, token: CancellationToken) -> None:
        self.clock = clock
        self.token = token
        self.phase: str = "INIT"
        self.finished: bool = False   # 由运行器在提交边界检查点后置位
        self.checkpoints: int = 0
        self._leases: List[_Lease] = []
        self._released: List[str] = []
        self._cleaning: bool = False
        token._listeners.append(self._on_signal)

    # ---- 安全检查点 -------------------------------------------------
    def checkpoint(self) -> None:
        """唯一允许响应中断的位置。规则：

        1. 先泵出所有到期事件（超时闹钟、到点的取消信号）；
        2. 任务已提交结果（finished）后，信号只记为 late，不再响应；
        3. 清理期间（_cleaning）不调用本方法，信号由监听器记为 deferred；
        4. 有挂起信号则抛出 Interrupt，否则正常返回。
        """
        self.checkpoints += 1
        self.clock.pump()
        if self.finished:
            return
        reason = self.token.pending_reason()
        if reason is not None:
            raise Interrupt(reason, self.phase, self.clock.now)

    def _on_signal(self) -> None:
        if self.finished:
            self.token.late += 1
        elif self._cleaning:
            self.token.deferred += 1

    # ---- 阶段标记 ----------------------------------------------------
    def enter_phase(self, name: str) -> None:
        self.phase = name

    # ---- 时间消耗（模拟工作 / 获取资源的耗时） ------------------------
    def work(self, dt: float, steps: int = 1) -> None:
        """推进 ``dt`` 的虚拟工作时间。``steps>1`` 时切成若干片，每片之间
        插入检查点——这是长循环的标准写法，保证响应延迟有界。"""
        if steps < 1:
            raise ValueError("steps must be >= 1")
        slice_dt = dt / steps
        for _ in range(steps):
            self.checkpoint()
            self.clock.advance(slice_dt)

    # ---- 资源获取与清理 ----------------------------------------------
    def acquire(
        self,
        name: str,
        release: Callable[[], None],
        cost: float = 0.0,
        on_acquire: Optional[Callable[[], None]] = None,
    ) -> None:
        """获取一个资源。获取动作本身是不可取消区（不插入检查点），
        顺序为：推进耗时 → on_acquire 登记 → 挂租约 → 过检查点。
        获取完成后立即过检查点：若此刻被中断，清理逻辑会负责释放它，
        因此不存在「拿到了却没人释放」的窗口。"""
        self.clock.advance(cost)
        if on_acquire is not None:
            on_acquire()
        self._leases.append(_Lease(name, release, self.clock.now))
        self.checkpoint()

    def run_cleanup(self) -> List[str]:
        """按获取逆序释放全部资源。整个清理是不可取消区：期间到达的
        信号只记为 deferred（任务已提交则记为 late），绝不打断清理。
        单个资源释放失败不影响其余资源的释放（异常被吞掉并继续），
        保证清理一定跑完。每释放一个资源后泵一次事件队列，仅为了让
        清理期间到期的信号被观测记录，而不是为了响应它们。"""
        self._cleaning = True
        try:
            while self._leases:
                lease = self._leases.pop()
                try:
                    lease.release()
                except Exception:
                    pass
                self._released.append(lease.name)
                self.clock.pump()
        finally:
            self._cleaning = False
        return list(self._released)

    # ---- 提交结果 ------------------------------------------------------
    def complete(self, value: Any) -> Any:
        """任务侧提交结果（通常是任务最后一句 ``return ctx.complete(v)``）。

        真正的 finished 状态由运行器在「提交边界检查点」通过之后才置位。
        因此恰好在提交时刻到期的信号仍会在边界检查点被响应
        （平局归中断）；严格晚于提交时刻的信号才记为 late。"""
        return value


@dataclass
class RunResult:
    outcome: Outcome
    clock: VirtualClock
    token: CancellationToken
    ctx: TaskContext


def run(
    task: Callable[[TaskContext], Any],
    *,
    deadline: Optional[float] = None,
    cancels: Tuple[Tuple[float, str], ...] = (),
) -> RunResult:
    """在注入时钟下运行一个协作式任务。

    - ``deadline``：虚拟时刻，到达即视为超时（``now >= deadline`` 时
      下一个检查点响应）。``deadline=0`` 表示立即超时。
    - ``cancels``：``(时刻, 标签)`` 序列，表示外部在指定虚拟时刻发起
      主动取消。
    """
    clock = VirtualClock()
    token = CancellationToken(clock, deadline)
    for at, _label in cancels:
        token.cancel(at=at)
    ctx = TaskContext(clock, token)

    kind: Kind
    value: Any = None
    error: Optional[str] = None
    phase: Optional[str] = None
    signal_at: Optional[float] = None  # 仅作“是否发生中断”哨兵
    responded_at: Optional[float] = None

    try:
        ctx.checkpoint()  # 入口检查点：deadline<=0 或预先取消在此响应
        value = task(ctx)
        ctx.checkpoint()  # 提交边界检查点：到期时刻<=提交时刻的信号仍生效（平局归中断）
        ctx.finished = True
        kind = Kind.OK
        phase = ctx.phase
    except Interrupt as intr:
        kind = intr.reason
        phase = intr.phase
        signal_at = token.latched_at
        responded_at = intr.at
    except Exception as exc:  # 任务自身失败，与中断无关
        kind = Kind.FAILED
        error = f"{type(exc).__name__}: {exc}"
        phase = ctx.phase

    released = ctx.run_cleanup()  # 不可取消区：任何路径都会完整清理
    outcome = Outcome(
        kind=kind,
        value=value if kind is Kind.OK else None,
        error=error,
        phase=phase,
        signal_at=(token.due_at if signal_at is not None else None),
        responded_at=responded_at,
        latency=(responded_at - token.due_at) if signal_at is not None else None,
        released=tuple(released),
        deferred=token.deferred,
        late=token.late,
    )
    return RunResult(outcome=outcome, clock=clock, token=token, ctx=ctx)
