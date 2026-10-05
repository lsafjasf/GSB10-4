"""txretry — 事务冲突重试库（死锁退避，带抖动，共享预算）。

仅依赖标准库。时间（时钟与睡眠）全部通过 Clock 注入，测试可完全复现。

冲突分类
--------
可重试（RetryClass.RETRYABLE）：
    事务自身无错，是并发调度导致的偶发失败，换一条时间线重跑即可成功。
    - PostgreSQL SQLSTATE 40P01  deadlock_detected
    - PostgreSQL SQLSTATE 40001  serialization_failure
    - PostgreSQL SQLSTATE 55P03  lock_not_available（NOWAIT 取锁失败）
    - MySQL 错误码 1213          ER_LOCK_DEADLOCK
    - MySQL 错误码 1205          ER_LOCK_WAIT_TIMEOUT
    - 任意以 "40" 开头的 SQLSTATE（transaction rollback 整类）
    - 驱动无关的 DeadlockError / SerializationError / LockTimeoutError

不可重试（RetryClass.FATAL）：
    重试多少次都会同样失败，或重试会造成二次伤害，必须立即失败。
    - 语义/约束类：唯一键冲突 23505、外键 23503、非空 23502、
      检查约束 23514、类型不匹配 42804、除零 22012
      （重放同一语句必然得到同一结果）
    - 权限类：42501 insufficient_privilege（重试不会突然有权限）
    - 连接类：08xxx connection exception（连接已断，重试同一事务无意义，
      应由上层重建连接后整体重跑）
    - 业务校验失败、显式 AbortError（如余额不足主动回滚）
    - 未知异常：默认不可重试（fail-closed，宁漏重试不错重试）

判定入口：classify(exc)。识别顺序：显式 retryable 属性 -> 类型 ->
SQLSTATE -> MySQL 厂商错误码 -> 默认 FATAL。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Sequence

__all__ = [
    "RetryClass",
    "RetryState",
    "RetryPolicy",
    "AttemptRecord",
    "RetryResult",
    "RetryBudgetExhausted",
    "RetryableConflictError",
    "DeadlockError",
    "SerializationError",
    "LockTimeoutError",
    "FatalTransactionError",
    "AbortError",
    "classify",
    "Clock",
    "RealClock",
    "FakeClock",
    "run_transaction",
]


# ---------------------------------------------------------------- 错误分类

class RetryClass(Enum):
    RETRYABLE = "retryable"
    FATAL = "fatal"


class RetryableConflictError(Exception):
    """驱动无关的可重试冲突基类。"""

    retryable = True

    def __init__(self, message: str = "", *, sqlstate: Optional[str] = None,
                 vendor_code: Optional[int] = None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.vendor_code = vendor_code


class DeadlockError(RetryableConflictError):
    """死锁被选中为牺牲者（PG 40P01 / MySQL 1213）。"""


class SerializationError(RetryableConflictError):
    """可串行化快照冲突（PG 40001）。"""


class LockTimeoutError(RetryableConflictError):
    """锁等待超时 / NOWAIT 取锁失败（PG 55P03 / MySQL 1205）。"""


class FatalTransactionError(Exception):
    """驱动无关的不可重试错误基类（约束、权限、连接等）。"""

    retryable = False


class AbortError(FatalTransactionError):
    """业务主动中止（如余额不足），重试无意义。"""


# SQLSTATE 前缀 -> 是否可重试。40xxx 整类可重试，其余常见类显式列出。
_RETRYABLE_SQLSTATE = {"40P01", "40001", "55P03"}
_RETRYABLE_SQLSTATE_PREFIXES = ("40",)
_FATAL_SQLSTATE_PREFIXES = (
    "22",   # data exception（除零、数值越界…）
    "23",   # integrity constraint violation（唯一/外键/非空/检查）
    "28",   # invalid authorization
    "42",   # syntax error or access rule violation（含权限 42501）
    "08",   # connection exception
    "0A",   # feature not supported
    "2D",   # invalid transaction termination
)
_RETRYABLE_VENDOR_CODES = {1205, 1213}          # MySQL 锁超时 / 死锁
_FATAL_VENDOR_CODES = {1062, 1451, 1452, 1048}  # 唯一键 / 外键 / 非空


def classify(exc: BaseException) -> RetryClass:
    """把任意异常归类为 RETRYABLE 或 FATAL。未知异常一律 FATAL。"""
    attr = getattr(exc, "retryable", None)
    if attr is True:
        return RetryClass.RETRYABLE
    if attr is False:
        return RetryClass.FATAL
    if isinstance(exc, RetryableConflictError):
        return RetryClass.RETRYABLE
    if isinstance(exc, FatalTransactionError):
        return RetryClass.FATAL

    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if isinstance(sqlstate, str) and sqlstate:
        if sqlstate in _RETRYABLE_SQLSTATE:
            return RetryClass.RETRYABLE
        if sqlstate.startswith(_RETRYABLE_SQLSTATE_PREFIXES):
            return RetryClass.RETRYABLE
        if sqlstate.startswith(_FATAL_SQLSTATE_PREFIXES):
            return RetryClass.FATAL
        return RetryClass.FATAL

    vendor = getattr(exc, "vendor_code", None)
    if vendor is None and hasattr(exc, "args") and len(exc.args) >= 1 \
            and isinstance(exc.args[0], int):
        vendor = exc.args[0]  # MySQL 驱动惯例：args[0] 是错误码
    if isinstance(vendor, int):
        if vendor in _RETRYABLE_VENDOR_CODES:
            return RetryClass.RETRYABLE
        if vendor in _FATAL_VENDOR_CODES:
            return RetryClass.FATAL
        return RetryClass.FATAL

    return RetryClass.FATAL


# ---------------------------------------------------------------- 时钟注入

class Clock:
    """时间与睡眠的唯一来源，测试注入 FakeClock 即可复现。"""

    def now(self) -> float:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class RealClock(Clock):
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock(Clock):
    """虚拟时钟：now() 返回虚拟时间，sleep() 只推进虚拟时间不阻塞。"""

    def __init__(self, start: float = 0.0):
        self.t = float(start)
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def advance(self, seconds: float) -> None:
        """模拟事务执行本身耗时。"""
        self.t += seconds


# ---------------------------------------------------------------- 策略与预算

class RetryState(Enum):
    SUCCESS = "success"            # 成功（可能经过若干次重试）
    EXHAUSTED = "exhausted"        # 持续冲突，达到 max_attempts 上限
    BUDGET_EXHAUSTED = "budget_exhausted"  # 共享时间预算耗尽
    FATAL = "fatal"                # 不可重试错误，直接失败


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略。预算（max_attempts / time_budget）由整个重试过程共享，
    不随轮次重置，因此总时长有硬上界。

    上界估算：总耗时 <= time_budget（墙钟），
    且尝试次数 <= max_attempts，退避间隔 <= max_delay。
    """

    max_attempts: int = 8          # 总尝试次数上限（含首次），共享预算
    base_delay: float = 0.010      # 首次退避基数（秒）
    max_delay: float = 1.0         # 单次退避上限（秒）
    time_budget: float = 5.0       # 整个重试过程的墙钟预算（秒），共享
    jitter: str = "full"           # "full" | "equal" | "none"

    def backoff_cap(self, attempt_index: int) -> float:
        """第 attempt_index 次失败后的退避上限：min(max_delay, base*2^i)。"""
        return min(self.max_delay, self.base_delay * (2 ** attempt_index))

    def compute_delay(self, attempt_index: int, rng: random.Random) -> float:
        """带抖动的退避时长。

        full : uniform(0, cap)            —— AWS 推荐，竞争下摊得最开
        equal: cap/2 + uniform(0, cap/2)  —— 保底一半，方差较小
        none : cap                        —— 无抖动（仅用于对照实验）
        """
        cap = self.backoff_cap(attempt_index)
        if self.jitter == "full":
            return rng.uniform(0.0, cap)
        if self.jitter == "equal":
            return cap / 2.0 + rng.uniform(0.0, cap / 2.0)
        if self.jitter == "none":
            return cap
        raise ValueError(f"unknown jitter mode: {self.jitter!r}")


@dataclass(frozen=True)
class AttemptRecord:
    attempt: int            # 第几次尝试（1 起）
    started_at: float       # 时钟读数
    error: Optional[BaseException]
    retry_class: Optional[RetryClass]
    delay_after: float      # 本次失败后睡眠的秒数（未睡眠为 0.0）


class RetryBudgetExhausted(Exception):
    """持续冲突达到上限的终态。保留最后一次失败原因。"""

    def __init__(self, state: RetryState, attempts: int,
                 last_error: BaseException, elapsed: float):
        self.state = state
        self.attempts = attempts
        self.last_error = last_error
        self.elapsed = elapsed
        super().__init__(
            f"{state.value}: gave up after {attempts} attempt(s), "
            f"elapsed={elapsed:.6f}s, last_error={last_error!r}")


@dataclass
class RetryResult:
    state: RetryState
    value: Any = None
    attempts: int = 0
    elapsed: float = 0.0
    budget_remaining: float = 0.0
    last_error: Optional[BaseException] = None
    history: list[AttemptRecord] = field(default_factory=list)

    @property
    def retries(self) -> int:
        return max(0, self.attempts - 1)


# ---------------------------------------------------------------- 执行器

def run_transaction(
    fn: Callable[[], Any],
    *,
    policy: Optional[RetryPolicy] = None,
    clock: Optional[Clock] = None,
    rng: Optional[random.Random] = None,
    on_retry: Optional[Callable[[AttemptRecord], None]] = None,
) -> RetryResult:
    """执行 fn，冲突时按 policy 退避重试。

    终态保证：
    - 成功 -> RetryState.SUCCESS
    - 不可重试错误 -> RetryState.FATAL，立即返回，不消耗退避
    - 尝试次数耗尽 -> RetryState.EXHAUSTED
    - 时间预算耗尽 -> RetryState.BUDGET_EXHAUSTED
    后三种失败均抛出 RetryBudgetExhausted（FATAL 除外，直接抛原异常），
    且 last_error 始终保留最后一次失败原因。
    """
    policy = policy or RetryPolicy()
    clock = clock or RealClock()
    rng = rng or random.Random()

    history: list[AttemptRecord] = []
    start = clock.now()
    last_error: Optional[BaseException] = None

    for attempt in range(1, policy.max_attempts + 1):
        # 尝试前检查：预算已耗尽就不再发起新尝试。
        # 注意：预算能约束的是"调度"（是否再试、睡多久）；已经进入 fn、
        # 正在执行的当次事务无法被撤销，因此墙钟总时长上界为
        # time_budget + 单次事务执行耗时（见 README「预算语义」）。
        if attempt > 1 and clock.now() - start >= policy.time_budget:
            raise RetryBudgetExhausted(
                RetryState.BUDGET_EXHAUSTED, attempt - 1,
                last_error, clock.now() - start) from last_error

        attempt_start = clock.now()
        try:
            value = fn()
        except BaseException as exc:  # noqa: BLE001 - 分类后决定去向
            kind = classify(exc)
            if kind is RetryClass.FATAL:
                history.append(AttemptRecord(attempt, attempt_start, exc, kind, 0.0))
                raise

            last_error = exc
            elapsed = clock.now() - start
            remaining = policy.time_budget - elapsed
            is_last_attempt = attempt >= policy.max_attempts

            if is_last_attempt:
                delay = 0.0
            else:
                delay = policy.compute_delay(attempt - 1, rng)
                if delay > remaining:
                    # 预算不够睡完这一觉：不再尝试，直接进入预算耗尽终态
                    history.append(AttemptRecord(attempt, attempt_start,
                                                 exc, kind, 0.0))
                    raise RetryBudgetExhausted(
                        RetryState.BUDGET_EXHAUSTED, attempt,
                        last_error, elapsed) from last_error

            record = AttemptRecord(attempt, attempt_start, exc, kind, delay)
            history.append(record)
            if on_retry is not None:
                on_retry(record)

            if is_last_attempt:
                raise RetryBudgetExhausted(
                    RetryState.EXHAUSTED, attempt, last_error,
                    clock.now() - start) from last_error

            clock.sleep(delay)
        else:
            return RetryResult(
                state=RetryState.SUCCESS,
                value=value,
                attempts=attempt,
                elapsed=clock.now() - start,
                budget_remaining=policy.time_budget - (clock.now() - start),
                history=history,
            )

    raise AssertionError("unreachable")  # pragma: no cover


def run_transaction_value(fn: Callable[[], Any], **kwargs: Any) -> Any:
    """便捷包装：成功返回 fn 的值，失败抛异常。"""
    return run_transaction(fn, **kwargs).value
