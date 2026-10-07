"""可观测的 FIFO 事件循环模拟器。

时钟完全由外部注入（VirtualClock），因此同一段运行在任何机器上
重放都会得到逐比特一致的运行记录，供 FairnessAnalyzer 离线分析。
"""

from collections import deque
import inspect


class VirtualClock:
    """单调虚拟时钟，时间只能由显式 advance() 或回调耗时推进。"""

    def __init__(self, start=0.0):
        self._now = float(start)

    def now(self):
        return self._now

    def advance(self, dt):
        if dt < 0:
            raise ValueError("virtual clock cannot go backwards")
        self._now += float(dt)
        return self._now


def default_site(depth=2):
    """以调用点（文件:行号）作为注册点标识。

    depth 指向调用 schedule() 的那一层。测试中建议显式传入
    site="..."，避免依赖具体文件布局。
    """
    frame = inspect.currentframe()
    for _ in range(depth):
        if frame is not None:
            frame = frame.f_back
    if frame is None:
        return "<unknown>"
    return f"{frame.f_code.co_filename}:{frame.f_lineno}"


class FairLoop:
    """FIFO 就绪队列事件循环。

    - schedule(cb, name=None, site=None) 注册回调，记录入队时刻 ready_ts；
    - run(n) 依次取出并执行 n 个回调（队列提前耗尽则提前返回）；
    - 回调返回数值 dt 表示“该回调占用了 dt 秒”，时钟随之推进；
    - 回调内可以再 schedule()（如周期性任务重注册自身），其就绪时刻
      取入队瞬间（位于占用者的执行窗口内）；
    - 若挂接了 FairnessMonitor，每次执行都会追加一条 RunRecord，
      记录成本为 O(1)：两个时钟读数 + 一次列表追加。
    """

    def __init__(self, clock=None, monitor=None):
        self.clock = clock if clock is not None else VirtualClock()
        self.monitor = monitor
        self._queue = deque()  # (reg_id, enqueue_ts)
        self._callbacks = []  # 注册号 -> (name, site, fn)

    def schedule(self, fn, name=None, site=None):
        if site is None:
            site = default_site(depth=2)
        if name is None:
            name = getattr(fn, "__name__", None) or f"cb#{len(self._callbacks)}"
        reg_id = len(self._callbacks)
        self._callbacks.append((name, site, fn))
        self._queue.append((reg_id, self.clock.now()))
        return reg_id

    def pending(self):
        return len(self._queue)

    def run(self, n=1):
        executed = 0
        while executed < n and self._queue:
            reg_id, ready_ts = self._queue.popleft()
            name, site, fn = self._callbacks[reg_id]
            start_ts = self.clock.now()
            if self.monitor is not None:
                self.monitor._on_dispatch(reg_id, name, site, ready_ts, start_ts)
            dt = fn()
            if dt is not None:
                self.clock.advance(dt)
            if self.monitor is not None:
                self.monitor._on_complete(self.clock.now())
            executed += 1
        return executed

    def drain(self):
        total = 0
        while self._queue:
            total += self.run(1)
        return total
