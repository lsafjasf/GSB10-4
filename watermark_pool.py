"""水位控制连接池（仅标准库）。

水位规则（详细说明见 README.md）：
- 扩容触发：利用率 >= high_water，或存在等待者；且规模 < max_size。
- 扩容限速（三重上界）：令牌桶 expand_rate 个 / expand_window 秒；
  单次 tick 最多 expand_step 条；单次最多新增 max_grow_ratio * 当前规模 条。
- 缩容触发：利用率 <= low_water 且有效规模（剔除 DRAINING）> min_size。
- 缩容条件（空闲判定依据）：连接 in_flight == 0 且 now - last_used >= idle_timeout；
  单次 tick 最多 shrink_step 条、最多 max_shrink_ratio * 当前规模 条，
  且有效规模不得低于 min_size。
- 防抖：low_water < high_water 构成滞回区间；任意方向调整后 cooldown 秒内不再调整。
- 在途安全：只关闭 in_flight == 0 的连接；IN_USE 连接可被 drain() 标记为
  DRAINING，等在途请求结清（in_flight 归零）后才关闭，请求不会被丢弃。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from enum import Enum


class ConnState(Enum):
    IDLE = "idle"
    IN_USE = "in_use"
    DRAINING = "draining"
    CLOSED = "closed"


class Conn:
    __slots__ = ("id", "state", "in_flight", "last_used", "born")

    def __init__(self, cid, now):
        self.id = cid
        self.state = ConnState.IDLE
        self.in_flight = 0
        self.last_used = now
        self.born = now


class PoolExhausted(TimeoutError):
    """acquire 超时：请求未被授予连接，可安全重试，不存在半授予状态。"""


class ReleasedConnError(RuntimeError):
    """释放了未知或不在借出状态的连接。"""


class Lease:
    """阻塞式 acquire 的返回值，支持 with 语法自动归还。"""

    def __init__(self, pool, cid):
        self._pool = pool
        self.conn_id = cid
        self._closed = False

    def release(self):
        if not self._closed:
            self._closed = True
            self._pool.release(self.conn_id)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class WatermarkPool:
    def __init__(self, *, min_size=2, max_size=64,
                 low_water=0.30, high_water=0.70,
                 expand_step=4, shrink_step=2,
                 expand_rate=8, expand_window=1.0,
                 max_grow_ratio=1.0, max_shrink_ratio=0.25,
                 idle_timeout=30.0, cooldown=5.0,
                 connect_fn=None, close_fn=None, time_fn=time.monotonic):
        if not (0.0 <= low_water < high_water <= 1.0):
            raise ValueError("需要 0 <= low_water < high_water <= 1（滞回区间）")
        if not (1 <= min_size <= max_size):
            raise ValueError("需要 1 <= min_size <= max_size")
        self.min_size = min_size
        self.max_size = max_size
        self.low_water = low_water
        self.high_water = high_water
        self.expand_step = expand_step
        self.shrink_step = shrink_step
        self.expand_rate = expand_rate
        self.expand_window = expand_window
        self.max_grow_ratio = max_grow_ratio
        self.max_shrink_ratio = max_shrink_ratio
        self.idle_timeout = idle_timeout
        self.cooldown = cooldown
        self._connect_fn = connect_fn
        self._close_fn = close_fn
        self._time = time_fn
        now = self._time()
        self._conns = {}
        self._idle = deque()
        self._next_id = 0
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._waiters = 0
        self._tokens = float(expand_rate)
        self._tokens_at = now
        self._last_scale = now - cooldown  # 允许第一次调整立即发生
        self.stats = {
            "acquired": 0, "released": 0,
            "expanded": 0, "shrunk": 0, "drained": 0,
            "scale_up_events": 0, "scale_down_events": 0,
            "suppressed_by_cooldown": 0, "suppressed_shrink_busy": 0,
            "acquire_timeouts": 0, "dropped": 0,
        }
        with self._lock:
            for _ in range(min_size):
                self._add_conn_locked(now)

    # ---------- 内部 ----------

    def _add_conn_locked(self, now):
        cid = self._next_id
        self._next_id += 1
        if self._connect_fn:
            self._connect_fn(cid)
        c = Conn(cid, now)
        self._conns[cid] = c
        self._idle.append(cid)
        return c

    def _close_conn_locked(self, c):
        assert c.in_flight == 0, "在途请求未结清，禁止关闭连接"
        c.state = ConnState.CLOSED
        del self._conns[c.id]
        if self._close_fn:
            self._close_fn(c.id)

    def _counts_locked(self):
        busy = sum(1 for c in self._conns.values()
                   if c.state is ConnState.IN_USE or c.state is ConnState.DRAINING)
        draining = sum(1 for c in self._conns.values()
                       if c.state is ConnState.DRAINING)
        return busy, len(self._conns), draining

    def _try_acquire_locked(self):
        while self._idle:
            cid = self._idle.popleft()
            c = self._conns.get(cid)
            if c is None or c.state is not ConnState.IDLE:
                continue  # 惰性清理已关闭/已摘除的空闲句柄
            c.state = ConnState.IN_USE
            c.in_flight += 1
            self.stats["acquired"] += 1
            return cid
        return None

    # ---------- 水位调度 ----------

    def tick(self, now=None):
        """水位评估入口：由后台线程或仿真循环周期性调用。"""
        now = self._time() if now is None else now
        with self._cond:
            # 令牌桶限速：expand_rate 个 / expand_window 秒
            dt = max(0.0, now - self._tokens_at)
            self._tokens = min(float(self.expand_rate),
                               self._tokens + self.expand_rate * dt / self.expand_window)
            self._tokens_at = now
            busy, size, draining = self._counts_locked()
            effective = size - draining  # draining 已摘除，不参与下限计算
            util = busy / size if size else 0.0
            want_expand = (util >= self.high_water or self._waiters > 0) \
                and size < self.max_size
            want_shrink = util <= self.low_water and effective > self.min_size
            if now - self._last_scale < self.cooldown:
                if want_expand or want_shrink:
                    self.stats["suppressed_by_cooldown"] += 1
                self._cond.notify_all()
                return
            if want_expand:
                budget = int(min(self.expand_step, int(self._tokens),
                                 self.max_size - size,
                                 max(1, int(size * self.max_grow_ratio))))
                if budget > 0:
                    for _ in range(budget):
                        self._add_conn_locked(now)
                    self._tokens -= budget
                    self._last_scale = now
                    self.stats["expanded"] += budget
                    self.stats["scale_up_events"] += 1
            elif want_shrink:
                # 空闲判定依据：状态 IDLE + in_flight == 0 + 空闲时长 >= idle_timeout
                candidates = []
                for cid in list(self._idle):
                    c = self._conns.get(cid)
                    if (c is not None and c.state is ConnState.IDLE
                            and c.in_flight == 0
                            and now - c.last_used >= self.idle_timeout):
                        candidates.append(c)
                self.stats["suppressed_shrink_busy"] += sum(
                    1 for c in self._conns.values()
                    if c.state is ConnState.IN_USE
                    and now - c.last_used >= self.idle_timeout)
                n = min(self.shrink_step, len(candidates),
                        effective - self.min_size,
                        max(1, int(size * self.max_shrink_ratio)))
                for c in candidates[:n]:
                    self._close_conn_locked(c)
                if n > 0:
                    self._idle = deque(
                        cid for cid in self._idle
                        if (cid in self._conns
                            and self._conns[cid].state is ConnState.IDLE))
                    self._last_scale = now
                    self.stats["shrunk"] += n
                    self.stats["scale_down_events"] += 1
            self._cond.notify_all()

    # ---------- 借还 ----------

    def try_acquire(self):
        """非阻塞借出：成功返回连接 id，无可用连接返回 None（请求入等待侧由调用方重试）。"""
        with self._cond:
            return self._try_acquire_locked()

    def acquire(self, timeout=None):
        """阻塞借出，返回 Lease；超时抛 PoolExhausted，不会半授予。"""
        deadline = None if timeout is None else self._time() + timeout
        with self._cond:
            while True:
                cid = self._try_acquire_locked()
                if cid is not None:
                    return Lease(self, cid)
                self._waiters += 1
                try:
                    self.tick()  # 有等待者时立即评估扩容
                    if deadline is None:
                        self._cond.wait()
                    else:
                        remaining = deadline - self._time()
                        if remaining <= 0:
                            self.stats["acquire_timeouts"] += 1
                            raise PoolExhausted("获取连接超时")
                        self._cond.wait(remaining)
                finally:
                    self._waiters -= 1

    def release(self, cid):
        """归还连接。在途计数归零后：DRAINING 连接关闭，其余回到空闲队列。"""
        now = self._time()
        with self._cond:
            c = self._conns.get(cid)
            if c is None or c.in_flight <= 0 or c.state is ConnState.CLOSED:
                raise ReleasedConnError(f"连接 {cid} 不在借出状态")
            c.in_flight -= 1
            c.last_used = now
            self.stats["released"] += 1
            if c.in_flight == 0:
                if c.state is ConnState.DRAINING:
                    self._close_conn_locked(c)
                    self.stats["drained"] += 1
                else:
                    c.state = ConnState.IDLE
                    self._idle.append(cid)
            self._cond.notify()

    def drain(self, cid):
        """摘除连接：空闲立即关闭；在途则标记 DRAINING，等请求安全结清后关闭。

        摘除属于强制移除：先补位再摘除（与真实池"先建后拆"一致），
        补位允许瞬时超过 max_size，超出部分以 DRAINING 数量为界，
        保证任意时刻有效规模（剔除 DRAINING）不低于 min_size。
        """
        with self._cond:
            c = self._conns.get(cid)
            if c is None:
                raise ReleasedConnError(f"连接 {cid} 不存在")
            if c.state in (ConnState.IDLE, ConnState.IN_USE):
                now = self._time()
                effective = len(self._conns) - sum(
                    1 for x in self._conns.values()
                    if x.state is ConnState.DRAINING)
                deficit = self.min_size - (effective - 1)
                for _ in range(max(0, deficit)):
                    self._add_conn_locked(now)
            if c.state is ConnState.IDLE:
                self._close_conn_locked(c)
                self.stats["drained"] += 1
            elif c.state is ConnState.IN_USE:
                c.state = ConnState.DRAINING
            self._cond.notify_all()

    # ---------- 观测 ----------

    def snapshot(self):
        with self._lock:
            busy, size, draining = self._counts_locked()
            idle = sum(1 for c in self._conns.values()
                       if c.state is ConnState.IDLE)
            return {
                "size": size, "busy": busy, "idle": idle,
                "waiters": self._waiters,
                "util": busy / size if size else 0.0,
            }

    def check_invariants(self):
        """对拍/测试用不变量校验。"""
        with self._lock:
            for c in self._conns.values():
                assert c.state is not ConnState.CLOSED
                if c.state is ConnState.IDLE:
                    assert c.in_flight == 0, "空闲连接在途计数必须为 0"
                if c.state is ConnState.IN_USE:
                    assert c.in_flight > 0, "在用连接在途计数必须为正"
            live_idle = [cid for cid in self._idle
                         if cid in self._conns
                         and self._conns[cid].state is ConnState.IDLE]
            n_idle = sum(1 for c in self._conns.values()
                         if c.state is ConnState.IDLE)
            assert len(live_idle) == n_idle, "空闲队列与连接表不一致"
            draining = sum(1 for c in self._conns.values()
                           if c.state is ConnState.DRAINING)
            assert len(self._conns) >= self.min_size, "规模跌破下限"
            # 总数允许因 drain 补位瞬时超过 max_size，但有效规模不可越上限
            assert len(self._conns) - draining <= self.max_size, "有效规模越上限"
            return True
