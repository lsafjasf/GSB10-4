"""连接池水位控制库（纯标准库，Python 3）。

水位规则（完整说明见 README.md）：

扩容（只允许在获取连接时发生，且受令牌桶限速）：
  1. 刚性触发：当前没有任何空闲连接可用；
  2. 预扩容触发：本次获取后繁忙占比 busy/size >= high_water；
  两种触发都必须拿到扩容令牌（突发 up_burst 个，之后每秒 up_rate 个），
  且 size < max_size。拿不到令牌时获取方进入等待/超时，形成背压。

缩容（在归还连接或调用 reap() 时评估，全部条件需同时满足）：
  1. busy/size < low_water（处于低水位）；
  2. 候选连接 in_flight == 0（无在途请求）；
  3. now - conn.last_used >= idle_timeout（空闲时长达标）；
  4. 距上次任意方向扩缩已过 cooldown，距上次缩容已过 down_interval；
  5. 每次最多缩 down_step 个，且 size 不会低于 min_size。
  关闭连接时断言 in_flight == 0，在途请求未结清的连接永不进入关闭路径。

震荡抑制：low_water < high_water 的滞回带 + 双向 cooldown + 缩容步进限速。

时间通过 time_fn 注入，便于用虚拟时钟做确定性自测。
"""
from __future__ import annotations

import threading
import time

IDLE = "idle"
IN_USE = "in_use"


class PoolExhausted(TimeoutError):
    """超时时间内无法获得连接（已触顶，或扩容令牌尚未补充）。"""


class _Conn:
    __slots__ = ("cid", "in_flight", "last_used", "state")

    def __init__(self, cid, now):
        self.cid = cid
        self.in_flight = 0
        self.last_used = now
        self.state = IDLE


class Handle:
    """一次获取的凭证，必须恰好归还一次。"""

    __slots__ = ("_pool", "_conn", "_released")

    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn
        self._released = False

    @property
    def conn_id(self):
        return self._conn.cid

    def release(self):
        if self._released:
            raise RuntimeError("handle already released")
        self._released = True
        self._pool._checkin(self._conn)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class WatermarkPool:
    def __init__(
        self,
        *,
        min_size=2,
        max_size=64,
        high_water=0.8,
        low_water=0.3,
        idle_timeout=5.0,
        up_burst=4,
        up_rate=8.0,
        down_step=1,
        down_interval=1.0,
        cooldown=2.0,
        time_fn=time.monotonic,
        event_log=None,
    ):
        if not (0.0 < low_water < high_water <= 1.0):
            raise ValueError("要求 0 < low_water < high_water <= 1（滞回带）")
        if not (0 < min_size <= max_size):
            raise ValueError("要求 0 < min_size <= max_size")
        if up_burst < 1 or up_rate < 0 or down_step < 1:
            raise ValueError("速率参数非法")
        if idle_timeout < 0 or down_interval < 0 or cooldown < 0:
            raise ValueError("时间参数非法")

        self.min_size = min_size
        self.max_size = max_size
        self.high_water = high_water
        self.low_water = low_water
        self.idle_timeout = idle_timeout
        self.up_burst = float(up_burst)
        self.up_rate = float(up_rate)
        self.down_step = down_step
        self.down_interval = down_interval
        self.cooldown = cooldown
        self._now = time_fn
        self._log = event_log.append if event_log is not None else (lambda _e: None)

        self._cond = threading.Condition()
        self._conns = {}
        self._next_cid = 0
        self._tokens = float(up_burst)
        self._last_refill = self._now()
        self._last_scale = float("-inf")
        self._last_down = float("-inf")

        now = self._now()
        for _ in range(min_size):
            c = _Conn(self._next_cid, now)
            self._next_cid += 1
            self._conns[c.cid] = c
        self._log((now, "bootstrap", None, min_size, 0, "min_size"))

    # ---------- 基础原语 ----------

    def _busy_ratio_locked(self):
        size = len(self._conns)
        busy = sum(1 for c in self._conns.values() if c.in_flight > 0)
        return busy / size, busy, size

    def _pick_idle_locked(self):
        # 优先复用最近使用过的连接，让冷连接自然老化，便于按 last_used 淘汰。
        best = None
        for c in self._conns.values():
            if c.state == IDLE and c.in_flight == 0:
                if best is None or c.last_used > best.last_used:
                    best = c
        return best

    def _take_token_locked(self):
        now = self._now()
        self._tokens = min(
            self.up_burst,
            self._tokens + (now - self._last_refill) * self.up_rate,
        )
        self._last_refill = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def _seconds_to_token_locked(self):
        if self._tokens >= 1.0:
            return 0.0
        if self.up_rate <= 0:
            return None
        return (1.0 - self._tokens) / self.up_rate

    def _grow_locked(self, why):
        now = self._now()
        c = _Conn(self._next_cid, now)
        self._next_cid += 1
        self._conns[c.cid] = c
        self._last_scale = now
        _, busy, size = self._busy_ratio_locked()
        self._log((now, "grow", c.cid, size, busy, why))
        return c

    def _close_locked(self, c, why):
        # 安全结清的硬性保证：在途请求不为 0 的连接绝不允许关闭。
        assert c.in_flight == 0, "缩容关闭连接时仍有在途请求"
        now = self._now()
        del self._conns[c.cid]
        _, busy, size = self._busy_ratio_locked()
        self._log((now, "close", c.cid, size, busy, why))

    def _checkout_locked(self, c):
        c.in_flight += 1
        c.state = IN_USE
        c.last_used = self._now()
        return Handle(self, c)

    # ---------- 获取 / 归还 ----------

    def acquire(self, timeout=None):
        with self._cond:
            deadline = None if timeout is None else self._now() + timeout
            while True:
                c = self._pick_idle_locked()
                if c is not None:
                    h = self._checkout_locked(c)
                    # 高水位预扩容（仅复用分支；刚扩完容的分支不重复触发）
                    self._maybe_topup_locked()
                    return h

                # 刚性扩容：无空闲可用，且未触顶、拿到令牌。
                if len(self._conns) < self.max_size and self._take_token_locked():
                    c = self._grow_locked("no_idle")
                    return self._checkout_locked(c)

                wait_s = self._wait_slice_locked(deadline)
                if wait_s <= 0:
                    raise PoolExhausted("连接池触顶或扩容被限速")
                self._cond.wait(wait_s)

    def _maybe_topup_locked(self):
        # 预扩容：本次获取后繁忙占比越过 high_water，在限速允许时补一个。
        if len(self._conns) >= self.max_size:
            return
        ratio, _, _ = self._busy_ratio_locked()
        if ratio >= self.high_water and self._take_token_locked():
            self._grow_locked("high_water")

    def _checkin(self, c):
        with self._cond:
            c.in_flight -= 1
            c.last_used = self._now()
            c.state = IDLE
            self._maybe_shrink_locked()
            self._cond.notify_all()

    # ---------- 缩容 ----------

    def reap(self):
        """由外部（如定时清理线程）驱动一次缩容评估，返回本次关闭数。"""
        with self._cond:
            n = self._maybe_shrink_locked()
            if n:
                self._cond.notify_all()
            return n

    def _maybe_shrink_locked(self):
        now = self._now()
        size = len(self._conns)
        if size <= self.min_size:
            return 0
        if now - self._last_scale < self.cooldown:
            return 0  # 双向冷却期：抑制水位震荡
        if now - self._last_down < self.down_interval:
            return 0  # 缩容步进限速
        ratio, _, _ = self._busy_ratio_locked()
        if ratio >= self.low_water:
            return 0  # 未跌破低水位
        # 空闲判定双依据：在途计数为 0 且 最后使用时间距今超过 idle_timeout。
        cold = [
            c
            for c in self._conns.values()
            if c.in_flight == 0 and now - c.last_used >= self.idle_timeout
        ]
        cold.sort(key=lambda x: x.last_used)  # 最久未用先淘汰
        n = min(self.down_step, len(cold), size - self.min_size)
        for c in cold[:n]:
            self._close_locked(c, "low_water")
        if n:
            self._last_down = now
            self._last_scale = now
        return n

    # ---------- 等待调度 ----------

    def _wait_slice_locked(self, deadline):
        now = self._now()
        if deadline is not None:
            remain = deadline - now
            if remain <= 0:
                return 0.0
        else:
            remain = None
        if len(self._conns) < self.max_size:
            t_token = self._seconds_to_token_locked()
            if t_token is not None:
                remain = t_token if remain is None else min(remain, t_token)
        if remain is None:
            remain = 0.05  # 触顶等待归还：周期性唤醒兜底
        return remain

    # ---------- 观测 ----------

    def stats(self):
        with self._cond:
            ratio, busy, size = self._busy_ratio_locked()
            return {
                "size": size,
                "busy": busy,
                "idle": size - busy,
                "in_flight": sum(c.in_flight for c in self._conns.values()),
                "busy_ratio": ratio,
                "tokens": self._tokens,
            }
