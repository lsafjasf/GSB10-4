"""线程安全的惰性初始化库（Python 3，仅标准库）。

提供两种实现：
  Lazy       —— 双重检查锁定（DCL）：初始化完成后走无锁快路径。
  LockedLazy —— 每次 get() 都加锁（正确但较慢，用于性能对比）。

共同语义
--------
* 恰好一次：无论多少线程并发触发，工厂函数成功执行只有一次的
  结果被发布；并发 get() 返回同一个对象。
* 无半成品：工厂先在局部变量里把对象完整构造好，再用一次原子
  赋值发布引用。其他线程要么读到 _UNSET（转而走慢路径等待），
  要么读到完整对象，绝不可能读到构造到一半的对象。
* 失败语义（构造参数 cache_exceptions）：
    False（默认）—— 失败是瞬时的：异常原样抛给本次调用者，
        状态保持未初始化，下一次 get() 会重试工厂函数。
    True         —— 失败是永久的：异常被缓存，之后每次 get()
        都重新抛出同一个异常对象，工厂不再执行。
* 递归访问：工厂执行期间同一线程再次 get() 立即抛 RuntimeError，
  而不是死锁。

内存可见性依据（CPython）
-------------------------
1. 慢路径：threading.Lock 的 release/acquire 构成 happens-before
   边——工厂在锁内完成构造并写 _state，释放锁；之后任何抢到锁的
   线程都能看到这次写入。这是 Python 文档保证的同步语义。
2. 快路径（无锁读）：CPython 的 GIL 保证对单个对象引用的读/写是
   原子的，不存在撕裂读；且发布动作是"构造完成之后的唯一一次赋
   值"，因此读到非 _UNSET 值时，该对象必然已构造完毕。
   注意：Python 语言规范本身没有定义内存模型，无锁快路径的正确性
   依赖 CPython（GIL）这一目标运行时的实现事实；在锁保护的路径上
   则不依赖任何实现细节。
"""

import threading

_UNSET = object()


class _Failed:
    """永久失败时缓存的异常载体。"""

    __slots__ = ("exc",)

    def __init__(self, exc):
        self.exc = exc


class _LazyBase:
    def __init__(self, factory, cache_exceptions=False):
        if not callable(factory):
            raise TypeError("factory must be callable")
        self._factory = factory
        self._cache_exceptions = cache_exceptions
        self._lock = threading.Lock()
        self._state = _UNSET  # _UNSET | 正常值 | _Failed
        self._owner = None    # 正在执行工厂函数的线程 ident

    def _check_not_recursive(self):
        # 在抢锁之前检查：若同线程递归进入，非可重入锁会死锁，
        # 必须抢锁前抛出。owner 只在工厂执行期间非 None。
        if self._owner == threading.get_ident():
            raise RuntimeError(
                "recursive get() during lazy initialization "
                "would deadlock; restructure the factory"
            )

    def _compute_locked(self):
        """在持有锁的前提下执行工厂并发布结果。"""
        self._owner = threading.get_ident()
        try:
            value = self._factory()
        except BaseException as exc:
            if self._cache_exceptions:
                self._state = _Failed(exc)  # 永久失败
            # 否则保持 _UNSET：瞬时失败，下次 get() 重试
            raise
        finally:
            self._owner = None
        # 发布：对象已在局部变量 value 中构造完毕，这里是一次原子赋值
        self._state = value
        return value

    @staticmethod
    def _unwrap(state):
        if isinstance(state, _Failed):
            raise state.exc
        return state


class Lazy(_LazyBase):
    """双重检查锁定：初始化后 get() 走无锁快路径。"""

    def get(self):
        state = self._state  # 快路径：一次原子读，不加锁
        if state is not _UNSET:
            return self._unwrap(state)
        return self._get_slow()

    def _get_slow(self):
        self._check_not_recursive()
        with self._lock:
            state = self._state
            if state is not _UNSET:
                return self._unwrap(state)
            return self._compute_locked()


class LockedLazy(_LazyBase):
    """每次 get() 都加锁的实现（用于与 Lazy 做性能对比）。"""

    def get(self):
        self._check_not_recursive()
        with self._lock:
            state = self._state
            if state is not _UNSET:
                return self._unwrap(state)
            return self._compute_locked()
