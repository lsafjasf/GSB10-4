"""长轮询超时判定与可复现重连退避（仅标准库）。

时间完全由外部注入：
  - clock: 任何带无参 ``now() -> float`` 的对象（秒）。
  - rng:   任何带 ``random() -> float``（返回 [0, 1)）的对象，
           生产环境可传 ``random.Random(seed)``，测试传 ``random.Random(固定种子)``，
           抖动序列在同一随机种子下逐位一致。

退避曲线（Equal Jitter，AWS 架构博客采用的半抖动形式）：
  n            = 连续失败次数（第 n 次失败后的等待，n 从 1 开始）
  raw(n)       = base_delay * factor ** (n - 1)
  capped(n)    = min(raw(n), max_delay)
  delay(n)     = capped(n) / 2 + rng.random() * capped(n) / 2

服务端重试提示（Retry-After，秒）：
  - 明确给出时优先于本地退避；
  - 但受 max_delay 硬上限钳制（服务端异常大的值不能击穿客户端上限）；
  - 失败计数照常 +1（提示只改等待时长，不改“连续失败”事实）；
  - 与本地退避冲突时的取舍：hint 有效（>=0）即用 hint，否则用本地抖动退避。

恢复条件：任意一次请求成功（success）即把连续失败计数清零，回到 IDLE。
放弃条件：连续失败达到 max_attempts 进入 GIVEN_UP，只能由 reset() 显式恢复。

时钟回拨：内部维护单调视图 mono_t，每次读到的原始时间若小于上次读数，
增量按 0 处理（mono_t 不回退），因此超时判定与重连时刻不会因回拨提前/重复触发。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Protocol


class State(str, Enum):
    IDLE = "IDLE"            # 空闲，未发起长轮询
    CONNECTING = "CONNECTING"  # 请求已发出，等待成功/超时/拒绝
    WAITING = "WAITING"      # 失败后等待退避窗口结束
    GIVEN_UP = "GIVEN_UP"    # 连续失败达到上界，停止调度


class FailReason(str, Enum):
    TIMEOUT = "timeout"
    REJECTED = "rejected"  # 服务端明确拒绝，可能携带 retry_after


class Clock(Protocol):
    def now(self) -> float: ...


class VirtualClock:
    """测试用虚拟时钟；支持前进与回拨。"""

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        self._t += float(seconds)
        return self._t

    def rollback(self, seconds: float) -> float:
        """显式把时钟往回拨，用于测试 NTP/人工改时间场景。"""
        self._t -= float(seconds)
        return self._t


@dataclass(frozen=True)
class BackoffPolicy:
    request_timeout: float = 30.0   # 单次长轮询超时（秒）
    base_delay: float = 1.0         # 首次失败的基础退避（秒）
    factor: float = 2.0             # 指数倍数
    max_delay: float = 60.0         # 退避硬上限（秒），含服务端提示
    max_attempts: int = 12          # 连续失败上界，达到即 GIVEN_UP

    def raw_delay(self, failures: int) -> float:
        """确定性的指数退避（未加抖动、未受失败上界约束前的值）。"""
        return self.base_delay * self.factor ** (failures - 1)

    def capped_delay(self, failures: int) -> float:
        return min(self.raw_delay(failures), self.max_delay)

    def jittered_delay(self, failures: int, rng_random: Callable[[], float]) -> float:
        """Equal Jitter：capped/2 + u(0, capped/2)，u 来自可注入随机源。"""
        capped = self.capped_delay(failures)
        return capped / 2.0 + rng_random() * capped / 2.0

    def resolve_delay(
        self, failures: int, rng_random: Callable[[], float],
        server_hint: Optional[float],
    ) -> float:
        """服务端提示优先，但一律钳制到 [0, max_delay]。"""
        if server_hint is not None and server_hint >= 0:
            return min(float(server_hint), self.max_delay)
        return self.jittered_delay(failures, rng_random)


class Reconnector:
    """长轮询连接/退避状态机，所有时间由注入时钟决定。"""

    __slots__ = ("policy", "_clock", "_rng_random", "state", "failures",
                 "_mono_t", "_last_raw", "_deadline", "next_due", "last_delay",
                 "last_reason")

    def __init__(self, clock: Clock, policy: Optional[BackoffPolicy] = None,
                 rng_random: Optional[Callable[[], float]] = None) -> None:
        import random as _random
        self.policy = policy or BackoffPolicy()
        self._clock = clock
        self._rng_random = rng_random if rng_random is not None else _random.Random().random
        self.state: State = State.IDLE
        self.failures: int = 0
        # 单调视图：原始时钟回拨时增量钳为 0
        self._mono_t = clock.now()
        self._last_raw = clock.now()
        self._deadline: Optional[float] = None  # CONNECTING 的超时时刻（单调视图）
        self.next_due: Optional[float] = None   # WAITING 结束后应重连的时刻（单调视图）
        self.last_delay: Optional[float] = None
        self.last_reason: Optional[str] = None

    # ---- 内部：读取时钟并维护单调视图 ----
    def _now_mono(self) -> float:
        raw = self._clock.now()
        self._mono_t += max(0.0, raw - self._last_raw)
        self._last_raw = raw
        return self._mono_t

    # ---- 动作 ----
    def start_poll(self) -> None:
        """IDLE/等待到期后发起一次长轮询。"""
        if self.state is State.WAITING and not self.wait_elapsed():
            raise RuntimeError("backoff window not elapsed yet")
        if self.state not in (State.IDLE, State.WAITING):
            raise RuntimeError(f"cannot start poll in state {self.state.value}")
        now = self._now_mono()
        self.state = State.CONNECTING
        self._deadline = now + self.policy.request_timeout
        self.next_due = None

    def on_success(self) -> None:
        if self.state is not State.CONNECTING:
            raise RuntimeError(f"unexpected success in state {self.state.value}")
        # 恢复条件：成功即清零
        self.failures = 0
        self.state = State.IDLE
        self._deadline = None
        self.next_due = None
        self.last_delay = None
        self.last_reason = None

    def on_failure(self, reason: FailReason,
                   server_hint: Optional[float] = None) -> float:
        """记录一次失败并进入退避；返回实际采用的等待秒数。"""
        if self.state is not State.CONNECTING:
            raise RuntimeError(f"unexpected failure in state {self.state.value}")
        now = self._now_mono()
        self.failures += 1
        self.last_reason = reason.value
        self._deadline = None
        if self.failures >= self.policy.max_attempts:
            self.state = State.GIVEN_UP
            self.last_delay = None
            self.next_due = None
            return math.inf
        delay = self.policy.resolve_delay(self.failures, self._rng_random, server_hint)
        self.last_delay = delay
        self.next_due = now + delay
        self.state = State.WAITING
        return delay

    def has_timed_out(self) -> bool:
        """超时判定：单调视图下 now >= 请求发起时刻 + request_timeout。"""
        if self.state is not State.CONNECTING or self._deadline is None:
            return False
        return self._now_mono() >= self._deadline

    def wait_elapsed(self) -> bool:
        if self.state is not State.WAITING or self.next_due is None:
            return False
        return self._now_mono() >= self.next_due

    def poll(self, server_hint: Optional[float] = None,
             fail: Optional[FailReason] = None) -> object:
        """测试/事件循环驱动的单步推进：

        - CONNECTING：先判超时；显式 fail 表示服务端拒绝/连接错误。
        - WAITING：窗口结束自动发起下一次请求。
        返回 ('success'|'timeout'|'rejected'|'waiting'|'reconnecting'|'given_up', delay)
        """
        now = self._now_mono()
        if self.state is State.IDLE:
            self.start_poll()
            return ("connecting", None)
        if self.state is State.CONNECTING:
            if fail is not None:
                delay = self.on_failure(fail, server_hint)
                if self.state is State.GIVEN_UP:
                    return ("given_up", None)
                return (fail.value, delay)
            if now >= (self._deadline or math.inf):
                delay = self.on_failure(FailReason.TIMEOUT)
                if self.state is State.GIVEN_UP:
                    return ("given_up", None)
                return ("timeout", delay)
            return ("waiting_response", None)
        if self.state is State.WAITING:
            if now >= (self.next_due or math.inf):
                self.start_poll()
                return ("reconnecting", None)
            return ("waiting", self.next_due - now)
        return (self.state.value.lower(), None)

    def reset(self) -> None:
        """从 GIVEN_UP（或任意状态）显式恢复：计数与状态归零。"""
        self.failures = 0
        self.state = State.IDLE
        self._deadline = None
        self.next_due = None
        self.last_delay = None
        self.last_reason = None
        self._now_mono()
