"""并发限制器：令牌获取/归还管理（仅标准库）。

提供两个实现：
- ConcurrencyLimiter：线程安全（threading.Condition），支持获取超时。
- AsyncConcurrencyLimiter：asyncio 版本，支持等待期间被取消。

保证：
1. 每条路径（成功 / 异常 / 取消 / 超时）的令牌去向都可核算；
2. 同一令牌重复归还会被检出并拒绝（DoubleReleaseError），计数不被污染；
3. 守恒恒等式恒成立：发放数 issued == 归还数 returned + 持有数 held。
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import threading
import time

__all__ = [
    "Token",
    "DoubleReleaseError",
    "InvalidTokenError",
    "AcquireTimeoutError",
    "ConcurrencyLimiter",
    "AsyncConcurrencyLimiter",
]


class LimiterError(Exception):
    """限制器错误基类。"""


class DoubleReleaseError(LimiterError):
    """同一令牌被重复归还时抛出。"""

    def __init__(self, token_id: int, first_return_seq: int, attempt_seq: int):
        self.token_id = token_id
        self.first_return_seq = first_return_seq
        self.attempt_seq = attempt_seq
        super().__init__(
            f"token #{token_id} already returned at global return sequence "
            f"#{first_return_seq}; duplicate return (attempt #{attempt_seq}) rejected"
        )


class InvalidTokenError(LimiterError):
    """归还了非本限制器发放的令牌（伪造或串用）。"""


class AcquireTimeoutError(LimiterError):
    """带超时的上下文管理器在超时时间内未拿到令牌。"""


class Token:
    """一次获取发放的唯一令牌；__slots__ 保证百万次场景下的内存上界。"""

    __slots__ = ("_limiter", "_token_id", "_released", "_return_seq")

    def __init__(self, limiter: object, token_id: int):
        self._limiter = limiter
        self._token_id = token_id
        self._released = False
        self._return_seq = -1

    @property
    def token_id(self) -> int:
        return self._token_id

    @property
    def released(self) -> bool:
        return self._released

    def __repr__(self) -> str:
        state = "returned" if self._released else "held"
        return f"Token(#{self._token_id}, {state})"


def _audit_check(capacity: int, available: int, held: int,
                 issued: int, returned: int) -> None:
    # 三条等价的守恒关系，任何一条不成立即内部计数错乱。
    assert 0 <= available <= capacity, (available, capacity)
    assert available + held == capacity, (available, held, capacity)
    assert issued == returned + held, (issued, returned, held)


class ConcurrencyLimiter:
    """线程安全的并发令牌限制器。"""

    def __init__(self, capacity: int):
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive int")
        self._capacity = capacity
        self._available = capacity
        self._held = 0
        self._issued_total = 0
        self._returned_total = 0
        self._next_token_id = 0
        self._double_release_attempts = 0
        self._invalid_release_attempts = 0
        self._cond = threading.Condition()

    @property
    def capacity(self) -> int:
        return self._capacity

    def _mint_locked(self) -> Token:
        self._next_token_id += 1
        self._issued_total += 1
        self._held += 1
        self._available -= 1
        _audit_check(self._capacity, self._available, self._held,
                     self._issued_total, self._returned_total)
        return Token(self, self._next_token_id)

    def acquire(self, timeout: float | None = None) -> Token | None:
        """获取令牌；超时返回 None（注意：超时不产生令牌，因此无需归还）。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._available == 0:
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            return self._mint_locked()

    def release(self, token: Token) -> int:
        """归还令牌；重复归还 / 非本限制器令牌会被拒绝。返回累计归还序号。"""
        if not isinstance(token, Token) or token._limiter is not self:
            with self._cond:
                self._invalid_release_attempts += 1
            raise InvalidTokenError("token was not issued by this limiter")
        with self._cond:
            if token._released:
                self._double_release_attempts += 1
                raise DoubleReleaseError(
                    token._token_id,
                    token._return_seq,
                    self._returned_total + 1,
                )
            token._released = True
            self._returned_total += 1
            token._return_seq = self._returned_total
            self._held -= 1
            self._available += 1
            _audit_check(self._capacity, self._available, self._held,
                         self._issued_total, self._returned_total)
            self._cond.notify()
            return token._return_seq

    @contextlib.contextmanager
    def hold(self, timeout: float | None = None):
        """上下文管理器：try/finally 保证异常路径也归还令牌。"""
        token = self.acquire(timeout)
        if token is None:
            raise AcquireTimeoutError(f"did not acquire token within {timeout}s")
        try:
            yield token
        finally:
            self.release(token)

    def audit(self) -> dict:
        """守恒核算快照。"""
        with self._cond:
            return {
                "capacity": self._capacity,
                "available": self._available,
                "held": self._held,
                "issued_total": self._issued_total,
                "returned_total": self._returned_total,
                "double_release_attempts": self._double_release_attempts,
                "invalid_release_attempts": self._invalid_release_attempts,
                "conserved": self._issued_total
                == self._returned_total + self._held,
            }


class AsyncConcurrencyLimiter:
    """asyncio 版本；单线程事件循环，用等待者队列唤醒，支持等待中取消。"""

    def __init__(self, capacity: int):
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive int")
        self._capacity = capacity
        self._available = capacity
        self._held = 0
        self._issued_total = 0
        self._returned_total = 0
        self._next_token_id = 0
        self._double_release_attempts = 0
        self._invalid_release_attempts = 0
        self._waiters: collections.deque = collections.deque()

    @property
    def capacity(self) -> int:
        return self._capacity

    def _mint(self) -> Token:
        self._next_token_id += 1
        self._issued_total += 1
        self._held += 1
        self._available -= 1
        _audit_check(self._capacity, self._available, self._held,
                     self._issued_total, self._returned_total)
        return Token(self, self._next_token_id)

    async def acquire(self, timeout: float | None = None) -> Token | None:
        """获取令牌；超时返回 None（不产生令牌）。等待中被取消则抛 CancelledError。"""
        if timeout is None:
            return await self._acquire_one()
        try:
            return await asyncio.wait_for(self._acquire_one(), timeout)
        except asyncio.TimeoutError:
            return None

    async def _acquire_one(self) -> Token:
        if not self._waiters and self._available > 0:
            return self._mint()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._waiters.append(fut)
        try:
            return await fut
        except BaseException:
            # 取消/超时与唤醒竞态：令牌已投递则立即归还，否则退出等待队列。
            if fut.done() and not fut.cancelled():
                self._release_token(fut.result())
            else:
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass
            raise

    def release(self, token: Token) -> int:
        """归还令牌（同步即可，asyncio 单线程内无数据竞争）。"""
        if not isinstance(token, Token) or token._limiter is not self:
            self._invalid_release_attempts += 1
            raise InvalidTokenError("token was not issued by this limiter")
        if token._released:
            self._double_release_attempts += 1
            raise DoubleReleaseError(
                token._token_id, token._return_seq, self._returned_total + 1
            )
        self._release_token(token)
        return token._return_seq

    def _release_token(self, token: Token) -> None:
        token._released = True
        self._returned_total += 1
        token._return_seq = self._returned_total
        self._held -= 1
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.cancelled():
                continue
            # 槽位移交唤醒者：先回流再铸令牌，净效果 available 不变。
            self._available += 1
            waiter.set_result(self._mint())
            return
        self._available += 1
        _audit_check(self._capacity, self._available, self._held,
                     self._issued_total, self._returned_total)

    @contextlib.asynccontextmanager
    async def hold(self, timeout: float | None = None):
        token = await self.acquire(timeout)
        if token is None:
            raise AcquireTimeoutError(f"did not acquire token within {timeout}s")
        try:
            yield token
        finally:
            self.release(token)

    def audit(self) -> dict:
        return {
            "capacity": self._capacity,
            "available": self._available,
            "held": self._held,
            "issued_total": self._issued_total,
            "returned_total": self._returned_total,
            "double_release_attempts": self._double_release_attempts,
            "invalid_release_attempts": self._invalid_release_attempts,
            "conserved": self._issued_total
            == self._returned_total + self._held,
        }
