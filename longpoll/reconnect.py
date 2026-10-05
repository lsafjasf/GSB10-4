"""长轮询超时与重连退避库（仅标准库，时间/抖动均可注入）。

核心契约
--------
* 时间不直接读系统时钟：所有时间来自注入的 ``clock``（秒，float）。
  测试可传入 :class:`VirtualClock`，生产可传入 :class:`MonotonicClock`。
* 抖动是注入的确定性随机源 :class:`SeedJitter`；相同 (种子, 调用次数)
  必然产生相同的抖动序列，因此每次重连时刻完全确定。
* 状态机为事件驱动单步迁移，不内置 sleep / 线程 / IO；
  测试只需拨动虚拟时钟并喂事件，即可精确核对每一次重连时刻。

状态: IDLE -> POLLING -> WAITING -> POLLING -> ... -> IDLE / FAILED
"""

from __future__ import annotations

import math
import random
import time
from enum import Enum

__all__ = [
    "State",
    "Intent",
    "Clock",
    "VirtualClock",
    "MonotonicClock",
    "Jitter",
    "SeedJitter",
    "RetryAfter",
    "ReconnectPolicy",
    "Reconnector",
]


class State(Enum):
    """重连状态机状态。"""

    IDLE = "IDLE"            # 从未开始 / reset 之后
    POLLING = "POLLING"      # 请求在途，等待服务端响应或超时
    WAITING = "WAITING"      # 失败后退避中，等待 reconnect_at
    FAILED = "FAILED"        # 连续失败达到上界，停止重连


class Intent(Enum):
    """事件处理后调用方应当执行的动作。"""

    SEND = "SEND"            # 立刻发起一次长轮询请求
    WAIT = "WAIT"            # 退避中，继续等到 state.reconnect_at
    GIVE_UP = "GIVE_UP"      # 达到连续失败上界，已进入 FAILED
    DONE = "DONE"             # 主动结束，回到 IDLE


class Clock:
    """时钟接口（鸭子类型即可，这里仅作文档基类）。"""

    def now(self) -> float:
        raise NotImplementedError


class MonotonicClock(Clock):
    """生产环境时钟：基于 time.monotonic()，不受系统墙上时间回拨影响。"""

    def now(self) -> float:
        return time.monotonic()


class VirtualClock(Clock):
    """测试用虚拟时钟：手动 advance，初始时刻可设（默认 1000.0）。"""

    __slots__ = ("_t",)

    def __init__(self, start: float = 1000.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("虚拟时钟不允许手动回拨")
        self._t += seconds
        return self._t


class Jitter:
    """抖动接口：返回 [0.0, 1.0) 上的一个值。"""

    def sample(self) -> float:
        raise NotImplementedError


class SeedJitter(Jitter):
    """种子化抖动：独立 random.Random 实例，跨进程同种子完全可复现。

    使用独立实例而非全局 random 模块，避免与被测代码其它随机源互相干扰。
    """

    __slots__ = ("_rng", "_calls")

    def __init__(self, seed: int):
        self._rng = random.Random(seed)
        self._calls = 0

    def sample(self) -> float:
        value = self._rng.random()
        self._calls += 1
        return value

    @property
    def calls(self) -> int:
        """已消耗的抖动样本数（用于测试核对）。"""
        return self._calls


class RetryAfter:
    """服务端显式重试提示（等价于 HTTP Retry-After，这里用秒）。"""

    __slots__ = ("delay", "permanent")

    def __init__(self, delay: float, permanent: bool = False):
        # 非法/缺失提示由调用方构造为 None；此处只接受非负有限值
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("retry-after delay 必须是非负有限秒数")
        self.delay = float(delay)
        self.permanent = bool(permanent)


class ReconnectPolicy:
    """退避策略（值对象，可复用于多个连接）。

    超时判定
    --------
    每次请求记录 deadline = start + request_timeout；时钟 now() >= deadline
    即判定超时（由调用方在 on_timeout 事件时触发，库不自行轮询时钟）。

    本地退避曲线（full jitter 之前先算确定性 base）
    -----------------------------------------------
    n = 失败次数（从 1 开始）
        base(n)   = min(base_delay * multiplier**(n-1), max_delay)
        sleep(n)  = random.uniform(0, base(n))               # Full Jitter
    即 sleep ∈ [0, base(n)]，均值为 base(n)/2，能打散同步重连风暴，
    且同一种子下序列唯一确定。

    服务端提示优先
    --------------
    若当次失败携带合法 Retry-After：
      * permanent=True（如 401/403 的明确语义）→ 直接进入 FAILED，不重试；
      * 否则 delay = min(hint, max_server_delay)，原样使用、不加抖动，
        但本次失败仍计入连续失败次数（达到上界依旧放弃）。
    服务端提示与本地曲线冲突时以服务端为准：服务端是负载的权威信息源；
    本地曲线仅在没有提示时兜底。上限保护防止错误/恶意的超长提示挂死客户端。

    连续失败上界
    ------------
    连续失败 max_failures 次后进入 FAILED，不再自动重连。
    任意一次成功（on_result 且 retriable=False）把计数清零。
    FAILED 只能由显式 reset() 恢复到 IDLE。
    """

    __slots__ = (
        "base_delay",
        "multiplier",
        "max_delay",
        "request_timeout",
        "max_failures",
        "max_server_delay",
    )

    def __init__(
        self,
        base_delay: float = 1.0,
        multiplier: float = 2.0,
        max_delay: float = 30.0,
        request_timeout: float = 25.0,
        max_failures: int = 8,
        max_server_delay: float = 300.0,
    ):
        if base_delay < 0 or multiplier < 1 or max_delay < base_delay:
            raise ValueError("退避参数非法")
        if request_timeout <= 0:
            raise ValueError("request_timeout 必须为正")
        if max_failures < 1:
            raise ValueError("max_failures 必须 >= 1")
        if max_server_delay < 0:
            raise ValueError("max_server_delay 不能为负")
        self.base_delay = float(base_delay)
        self.multiplier = float(multiplier)
        self.max_delay = float(max_delay)
        self.request_timeout = float(request_timeout)
        self.max_failures = int(max_failures)
        self.max_server_delay = float(max_server_delay)

    def base_backoff(self, failures: int) -> float:
        """第 failures 次失败的确定性 base 秒数（未加抖动）。

        在 log 域做封顶判定，避免 failures 很大时幂运算浮点溢出。
        """
        if failures < 1:
            raise ValueError("failures 必须 >= 1")
        if (self.base_delay == 0.0 or self.multiplier <= 1.0
                or self.base_delay >= self.max_delay):
            return min(self.base_delay, self.max_delay)
        threshold = (math.log(self.max_delay / self.base_delay)
                     / math.log(self.multiplier))
        if failures - 1 >= threshold:
            return self.max_delay
        return min(self.base_delay * (self.multiplier ** (failures - 1)),
                   self.max_delay)

    def local_backoff(self, failures: int, jitter: Jitter) -> float:
        """本地 full-jitter 退避秒数（failures 为当前失败序号，>=1）。"""
        return jitter.sample() * self.base_backoff(failures)


class Reconnector:
    """事件驱动的重连状态机。单线程使用；不含任何线程/IO。"""

    __slots__ = (
        "policy",
        "clock",
        "jitter",
        "state",
        "failures",
        "request_started_at",
        "deadline",
        "reconnect_at",
        "last_error",
        "_clock_skew_tolerance",
        "_last_now",
    )

    def __init__(
        self,
        policy: ReconnectPolicy,
        clock: Clock,
        jitter: Jitter,
        clock_skew_tolerance: float = 1e-6,
    ):
        self.policy = policy
        self.clock = clock
        self.jitter = jitter
        self.state = State.IDLE
        self.failures = 0
        self.request_started_at = None
        self.deadline = None
        self.reconnect_at = None
        self.last_error = None
        # 浮点/同机微小时钟抖动容忍；超过即视为回拨故障
        self._clock_skew_tolerance = float(clock_skew_tolerance)
        self._last_now = None

    # ---- 内部工具 -------------------------------------------------------

    def _guard_clock(self):
        """时钟回拨保护：与上一次观测到的时钟值比较，必须单调不减。"""
        now = self.clock.now()
        if (self._last_now is not None
                and now < self._last_now - self._clock_skew_tolerance):
            self.state = State.FAILED
            self.last_error = "clock went backwards: %r -> %r" % (
                self._last_now, now)
            raise ClockMovedBackwards(self.last_error)
        self._last_now = now
        return now

    def _enter_waiting(self, now: float, delay: float, error: str) -> Intent:
        self.failures += 1
        self.last_error = error
        if self.failures >= self.policy.max_failures:
            self.state = State.FAILED
            self.reconnect_at = None
            self.deadline = None
            return Intent.GIVE_UP
        if delay < 0:
            delay = 0.0
        self.reconnect_at = now + delay
        self.state = State.WAITING
        self.deadline = None
        return Intent.WAIT

    # ---- 事件 -----------------------------------------------------------

    def start(self) -> Intent:
        """从 IDLE 发起首次连接。"""
        if self.state is not State.IDLE:
            raise IllegalTransition("start 仅允许在 IDLE 调用，当前 %s" % self.state)
        now = self._guard_clock()
        return self._begin_poll(now)

    def on_tick(self) -> Intent:
        """推进状态：WAITING 到期则重连；其它状态原样返回应做动作。"""
        now = self._guard_clock()
        if self.state is State.WAITING:
            if now + self._clock_skew_tolerance >= self.reconnect_at:
                return self._begin_poll(now)
            return Intent.WAIT
        if self.state is State.POLLING:
            # 到期判定也可由调用方在此时统一处理，这里保持单步语义
            return Intent.WAIT
        if self.state in (State.IDLE, State.FAILED):
            return Intent.GIVE_UP if self.state is State.FAILED else Intent.DONE
        raise IllegalTransition("未知状态 %s" % self.state)

    def on_result(self, retriable: bool = False, retry_after=None) -> Intent:
        """收到服务端响应。

        retriable=False : 正常响应（含长轮询服务端正常超时返回）→ 成功，
                          失败计数清零并立刻发起下一次长轮询。
        retriable=True  : 服务端拒绝/可重试错误（如 503），携带 RetryAfter。
        retry_after     : RetryAfter 实例或 None。
        """
        if self.state is not State.POLLING:
            raise IllegalTransition("on_result 仅允许在 POLLING 调用，当前 %s"
                                    % self.state)
        now = self._guard_clock()
        if not retriable:
            return self._recover(now)
        return self._handle_rejection(now, retry_after)

    def on_timeout(self) -> Intent:
        """本地请求超时（now >= request_started_at + request_timeout）。"""
        if self.state is not State.POLLING:
            raise IllegalTransition("on_timeout 仅允许在 POLLING 调用，当前 %s"
                                    % self.state)
        now = self._guard_clock()
        elapsed = now - self.request_started_at
        if elapsed + self._clock_skew_tolerance < self.policy.request_timeout:
            raise IllegalTransition(
                "尚未超时: elapsed=%r < timeout=%r"
                % (elapsed, self.policy.request_timeout))
        delay = self.policy.local_backoff(self.failures + 1, self.jitter)
        return self._enter_waiting(now, delay, "timeout after %.3fs" % elapsed)

    def on_network_error(self, retry_after=None) -> Intent:
        """网络层失败（连接断开等）。可选携带服务端/代理给的 RetryAfter。"""
        if self.state is not State.POLLING:
            raise IllegalTransition("on_network_error 仅允许在 POLLING 调用，"
                                    "当前 %s" % self.state)
        now = self._guard_clock()
        delay = self._delay_for_failure(now, retry_after)
        return self._enter_waiting(now, delay, "network error")

    def _handle_rejection(self, now: float, retry_after) -> Intent:
        if isinstance(retry_after, RetryAfter) and retry_after.permanent:
            self.failures += 1
            self.last_error = "permanent rejection"
            self.state = State.FAILED
            self.reconnect_at = None
            self.deadline = None
            return Intent.GIVE_UP
        delay = self._delay_for_failure(now, retry_after)
        return self._enter_waiting(now, delay, "server rejection")

    def _delay_for_failure(self, now: float, retry_after) -> float:
        if isinstance(retry_after, RetryAfter) and not retry_after.permanent:
            return min(retry_after.delay, self.policy.max_server_delay)
        # 无合法提示 → 本地 full-jitter 曲线兜底
        return self.policy.local_backoff(self.failures + 1, self.jitter)

    def stop(self) -> Intent:
        """主动终止：任何活动状态回到 IDLE，失败计数保留与否由 reset 决定。"""
        self.state = State.IDLE
        self.deadline = None
        self.reconnect_at = None
        self.request_started_at = None
        return Intent.DONE

    def reset(self):
        """从 FAILED（或任意状态）彻底恢复：回 IDLE、清零计数。"""
        self.state = State.IDLE
        self.failures = 0
        self.request_started_at = None
        self.deadline = None
        self.reconnect_at = None
        self.last_error = None
        self._last_now = None

    # ---- 状态切换 -------------------------------------------------------

    def _begin_poll(self, now: float) -> Intent:
        self.state = State.POLLING
        self.request_started_at = now
        self.deadline = now + self.policy.request_timeout
        self.reconnect_at = None
        return Intent.SEND

    def _recover(self, now: float) -> Intent:
        """成功：清零连续失败并立刻发起下一轮长轮询。"""
        self.failures = 0
        self.last_error = None
        return self._begin_poll(now)


class ReconnectError(Exception):
    """库内错误基类。"""


class ClockMovedBackwards(ReconnectError):
    """注入时钟出现非单调（回拨）：立即进入 FAILED，需 reset。"""


class IllegalTransition(ReconnectError):
    """事件与当前状态不匹配，或超时提前触发。"""
