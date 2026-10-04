"""请求链路超时预算切分库（仅标准库）。

时间单位：毫秒。时钟由外部注入，默认使用 time.monotonic（秒换算为毫秒）。

核心模型
--------
1. 入口即建预算：Budget 在请求进入系统时创建，deadline = start + total_ms。
2. 绝对截止时间：每一段、每一次重试都用同一个绝对 deadline 计算剩余，
   排队等待、退避睡眠、下游执行的真实流逝时间全部计入同一份预算。
3. 权重切分：第 i 段分得
       remaining_at_stage_start * weight_i / sum(weight_i..n)
   即"剩余预算 / 剩余权重"动态切分；前段没用完的预算自动留给后段，
   不需要回收。
4. 硬上限 + 最小可用值：每次尝试的超时再受 max_attempt_ms / min_attempt_ms 约束；
   分不到正预算或低于最小可用值时，段开始前即判定耗尽，不发起任何调用。
5. 重试共享预算：同一段的第 k 次尝试只在"该段切片"内被再切分（默认平均），
   退避也消耗预算；预算为 0 立即停止，重试次数不会让总时长超过 total_ms。
6. 合作式执行：段函数必须遵守传入的 attempt_timeout_ms；若超过全局
   deadline 返回，记为 overrun，但后续段绝不会被调度（不会再产生新的越界）。
7. 时钟回拨：采用"包络（monotonic envelope）"口径——只增不减，
   observed_now = max(raw_clock(), 上次观察值)，回拨不会让预算凭空变多。

预算耗尽永远显式返回 ChainResult(status="exhausted" | "stage_timeout")，
绝不静默退化为"没有超时"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence


def _default_clock_ms() -> float:
    return time.monotonic() * 1000.0


def _default_sleep_ms(ms: float) -> None:
    time.sleep(ms / 1000.0)


@dataclass
class TimeBudget:
    """一份绝对截止时间预算；时间口径为包络单调时钟（毫秒）。"""

    total_ms: float
    deadline_ms: float
    _clock: Callable[[], float] = field(repr=False, default=_default_clock_ms)
    _sleep: Callable[[float], None] = field(repr=False, default=None)
    _last_observed: float = field(default=0.0, init=False)

    @classmethod
    def start(
        cls,
        total_ms: float,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> "TimeBudget":
        clock = clock or _default_clock_ms
        now = float(clock())
        budget = cls(
            total_ms=float(total_ms),
            deadline_ms=now + float(total_ms),
            _clock=clock,
            _sleep=sleep or _default_sleep_ms,
        )
        budget._last_observed = now
        return budget

    def now(self) -> float:
        """包络时间：raw 时钟回拨时沿用上一次观察值。"""
        raw = float(self._clock())
        observed = raw if raw > self._last_observed else self._last_observed
        self._last_observed = observed
        return observed

    def elapsed_ms(self) -> float:
        return self.now() - (self.deadline_ms - self.total_ms)

    def remaining_ms(self) -> float:
        return self.deadline_ms - self.now()

    def is_exhausted(self) -> bool:
        return self.remaining_ms() <= 0

    def sleep_budget(self, ms: float) -> None:
        """睡眠不超过剩余预算：剩余不足时只睡剩余部分（调用方随后会看到 0）。"""
        bounded = min(ms, max(0.0, self.remaining_ms()))
        if bounded > 0:
            self._sleep(bounded)


@dataclass(frozen=True)
class Stage:
    """链路中的一段。

    handler(ctx, attempt_timeout_ms) -> 任意返回值；
    下游超时时应抛 TimeoutError（与内置 TimeoutError 同类型）。
    """

    name: str
    handler: Callable[[dict, float], object]
    weight: float = 1.0
    max_attempts: int = 1
    max_attempt_ms: Optional[float] = None
    min_attempt_ms: float = 0.0
    backoff_base_ms: float = 0.0
    backoff_factor: float = 2.0


@dataclass
class ChainResult:
    status: str  # ok / exhausted / stage_timeout / stage_error
    stage_index: Optional[int] = None
    stage_name: Optional[str] = None
    values: List[object] = field(default_factory=list)
    attempts_by_stage: List[int] = field(default_factory=list)
    elapsed_ms: float = 0.0
    remaining_ms: float = 0.0
    reason: str = ""
    error: Optional[str] = None
    events: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "stage_index": self.stage_index,
            "stage_name": self.stage_name,
            "values": self.values,
            "attempts_by_stage": self.attempts_by_stage,
            "elapsed_ms": self.elapsed_ms,
            "remaining_ms": self.remaining_ms,
            "reason": self.reason,
            "error": self.error,
            "events": self.events,
        }


def run_chain(
    budget: TimeBudget,
    stages: Sequence[Stage],
    queue_wait_ms: float = 0.0,
) -> ChainResult:
    """在一份预算内顺序执行链路；排队等待计入预算。

    返回 ChainResult，绝不静默降级。
    """

    events: List[dict] = []
    values: List[object] = []
    attempts_log: List[int] = []

    def log(kind: str, **payload) -> None:
        event = {"kind": kind, "at_ms": round(budget.elapsed_ms(), 6)}
        event.update(payload)
        events.append(event)

    log("chain_start", total_ms=budget.total_ms, stages=len(stages))

    if queue_wait_ms > 0:
        log("queue_wait", requested_ms=queue_wait_ms)
        budget.sleep_budget(queue_wait_ms)

    weights = [max(0.0, s.weight) for s in stages]
    suffix = [0.0] * (len(weights) + 1)
    for i in range(len(weights) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + weights[i]

    for index, stage in enumerate(stages):
        remaining = budget.remaining_ms()
        log("stage_enter", stage=stage.name, index=index, remaining_ms=round(remaining, 6))

        if remaining <= 0:
            log("stop_before_stage", stage=stage.name, reason="budget_exhausted")
            return ChainResult(
                "exhausted", index, stage.name, values, attempts_log,
                budget.elapsed_ms(), budget.remaining_ms(),
                "budget_exhausted_before_stage", events=events,
            )

        stage_slice = remaining * weights[index] / suffix[index] if suffix[index] > 0 else remaining
        attempts = 0
        ctx = {"stage_index": index, "stage_name": stage.name}

        while attempts < stage.max_attempts:
            attempts += 1
            attempts_left = stage.max_attempts - attempts
            if attempts_left > 0:
                attempt_slice = stage_slice / (attempts_left + 1)
            else:
                attempt_slice = stage_slice

            grant = min(budget.remaining_ms(), attempt_slice)
            if stage.max_attempt_ms is not None:
                grant = min(grant, stage.max_attempt_ms)

            if grant <= 0:
                log("stop_attempt", stage=stage.name, attempt=attempts,
                    reason="no_budget", grant_ms=0)
                return ChainResult(
                    "exhausted", index, stage.name, values, attempts_log + [attempts],
                    budget.elapsed_ms(), budget.remaining_ms(),
                    "budget_exhausted_before_attempt", events=events,
                )

            if grant < stage.min_attempt_ms:
                log("stop_attempt", stage=stage.name, attempt=attempts,
                    reason="below_min_attempt", grant_ms=round(grant, 6),
                    min_attempt_ms=stage.min_attempt_ms)
                return ChainResult(
                    "exhausted", index, stage.name, values, attempts_log + [attempts],
                    budget.elapsed_ms(), budget.remaining_ms(),
                    "below_min_attempt_ms", events=events,
                )

            log("attempt_start", stage=stage.name, attempt=attempts,
                grant_ms=round(grant, 6), attempts_left=attempts_left)
            attempts_start = budget.elapsed_ms()
            try:
                value = stage.handler(ctx, grant)
                used = budget.elapsed_ms() - attempts_start
                log("attempt_ok", stage=stage.name, attempt=attempts,
                    used_ms=round(used, 6))

                if budget.remaining_ms() < 0:
                    log("overrun", stage=stage.name, attempt=attempts,
                        over_ms=round(-budget.remaining_ms(), 6))
                    values.append(value)
                    return ChainResult(
                        "exhausted", index, stage.name, values, attempts_log + [attempts],
                        budget.elapsed_ms(), budget.remaining_ms(),
                        "handler_overran_global_deadline", events=events,
                    )

                values.append(value)
                stage_slice -= used
                attempts_log.append(attempts)
                break
            except TimeoutError as exc:
                used = budget.elapsed_ms() - attempts_start
                log("attempt_timeout", stage=stage.name, attempt=attempts,
                    used_ms=round(used, 6))

                if budget.remaining_ms() <= 0:
                    log("deadline_hit", stage=stage.name)
                    return ChainResult(
                        "exhausted", index, stage.name, values, attempts_log + [attempts],
                        budget.elapsed_ms(), budget.remaining_ms(),
                        "global_deadline_after_attempt_timeout",
                        events=events,
                    )

                if attempts >= stage.max_attempts:
                    return ChainResult(
                        "stage_timeout", index, stage.name, values,
                        attempts_log + [attempts],
                        budget.elapsed_ms(), budget.remaining_ms(),
                        "attempts_exhausted_with_budget_remaining",
                        events=events,
                    )

                backoff = stage.backoff_base_ms * (
                    stage.backoff_factor ** (attempts - 1)
                )
                log("backoff", stage=stage.name, requested_ms=round(backoff, 6))
                before_sleep = budget.elapsed_ms()
                budget.sleep_budget(backoff)
                slept = budget.elapsed_ms() - before_sleep
                log("backoff_done", stage=stage.name, slept_ms=round(slept, 6))
                stage_slice -= used + slept

                if budget.is_exhausted():
                    return ChainResult(
                        "exhausted", index, stage.name, values,
                        attempts_log + [attempts],
                        budget.elapsed_ms(), budget.remaining_ms(),
                        "budget_exhausted_during_backoff", events=events,
                    )
            except Exception as exc:  # 非超时错误：显式失败，不静默、不重试
                log("attempt_error", stage=stage.name, attempt=attempts,
                    error=type(exc).__name__)
                return ChainResult(
                    "stage_error", index, stage.name, values,
                    attempts_log + [attempts],
                    budget.elapsed_ms(), budget.remaining_ms(),
                    "handler_raised", error=type(exc).__name__ + ": " + str(exc),
                    events=events,
                )
        else:  # pragma: no cover - 循环结构上的防御性分支
            return ChainResult(
                "stage_timeout", index, stage.name, values, attempts_log,
                budget.elapsed_ms(), budget.remaining_ms(),
                "attempts_exhausted", events=events,
            )

    log("chain_ok", remaining_ms=round(budget.remaining_ms(), 6))
    return ChainResult(
        "ok", None, None, values, attempts_log,
        budget.elapsed_ms(), budget.remaining_ms(), "", events=events,
    )


class FakeClock:
    """测试用虚拟时钟（毫秒整数）；sleep 直接推进虚拟时间，不消耗墙钟。"""

    def __init__(self, start_ms: float = 0) -> None:
        self.t = float(start_ms)

    def __call__(self) -> float:
        return self.t

    def sleep(self, ms: float) -> None:
        self.t += ms

    def advance(self, ms: float) -> None:
        self.t += ms

    def set(self, ms: float) -> None:
        self.t = float(ms)
