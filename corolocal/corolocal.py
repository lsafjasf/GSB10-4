"""协程本地存储（Coroutine-Local Storage）：派生时复制（Copy-On-Spawn）。

设计要点
========
1. 每个协程任务在自己的 ``contextvars.Context`` 里持有一个独立的
   :class:`Store`。``asyncio.create_task`` 会复制当前 Context，因此
   容器（Store 对象本身）天然隔离；但存进去的 *可变值*（dict/list/自定义
   对象）默认只是同一个引用，子任务里改值会串回父任务。
2. 本库在每个派生点（:func:`spawn`）对父 Store 做一次
   ``copy.deepcopy`` 快照，子任务拿到的是完全独立的副本 —— 子任务内
   对值的任何修改（包括原地修改可变对象）都不会影响父任务或兄弟任务。
3. 仅支持标准库，仅保证单线程、单事件循环内的正确性。asyncio 协程
   不会被抢占，单个任务内的 get/set 是原子的，不存在脏读。
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
from typing import Any, Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")

_MISSING = object()

_current: contextvars.ContextVar[Optional["Store"]] = contextvars.ContextVar(
    "corolocal_current_store", default=None
)


class Store:
    """一个协程本地的键值存储。通常通过 :func:`spawn` 隐式创建。"""

    __slots__ = ("_data", "_generation")

    def __init__(self) -> None:
        self._data: dict[Any, Any] = {}
        self._generation: int = 0

    def get(self, key: Any, default: Any = None) -> Any:
        """读取键。不存在时返回 ``default``（默认 None），不抛异常。"""
        return self._data.get(key, default)

    def require(self, key: Any) -> Any:
        """读取键；不存在时抛 :class:`KeyError`（确定行为，不返回脏值）。"""
        if key not in self._data:
            raise KeyError(key)
        return self._data[key]

    def set(self, key: Any, value: Any) -> None:
        """写入键。"""
        self._data[key] = value
        self._generation += 1

    def delete(self, key: Any) -> None:
        """删除键；不存在时抛 :class:`KeyError`。"""
        del self._data[key]
        self._generation += 1

    def has(self, key: Any) -> bool:
        return key in self._data

    def keys(self) -> tuple:
        return tuple(self._data.keys())

    def as_dict(self) -> dict[Any, Any]:
        """返回当前内容的 *浅* 拷贝快照（键值引用不变，仅容器独立）。"""
        return dict(self._data)

    def clone(self) -> "Store":
        """深拷贝整个 Store：键、值、嵌套可变对象全部独立。"""
        new = Store.__new__(Store)
        new._data = copy.deepcopy(self._data)
        new._generation = self._generation
        return new

    @property
    def generation(self) -> int:
        """写入代数：每次 set/delete 加 1，可用于可观测的一致性检查。"""
        return self._generation

    def __repr__(self) -> str:
        return f"Store({self._data!r})"


def current_store() -> Optional[Store]:
    """返回当前协程绑定的 Store；从未初始化（无上下文）时返回 None。"""
    return _current.get()


def get(key: Any, default: Any = None) -> Any:
    store = _current.get()
    return default if store is None else store.get(key, default)


def require(key: Any) -> Any:
    store = _current.get()
    if store is None:
        raise KeyError(f"no coroutine-local store in this context: {key!r}")
    return store.require(key)


def set(key: Any, value: Any) -> None:
    """在当前 Store 中写值。无上下文时惰性创建一个绑定到当前 Context 的
    根 Store（确定行为：之后同一 Context 内能读到，其它任务读不到）。"""
    store = _current.get()
    if store is None:
        store = Store()
        _current.set(store)
    store.set(key, value)


def delete(key: Any) -> None:
    store = _current.get()
    if store is None:
        raise KeyError(f"no coroutine-local store in this context: {key!r}")
    store.delete(key)


def has(key: Any) -> bool:
    store = _current.get()
    return False if store is None else store.has(key)


def fork() -> Store:
    """显式派生：返回当前 Store 的独立深拷贝；无上下文时返回空 Store。"""
    store = _current.get()
    return Store() if store is None else store.clone()


async def use_store(store: Store, coro_func: Callable[..., Awaitable[T]],
                    *args: Any, **kwargs: Any) -> T:
    """在 ``store`` 的深拷贝上运行协程，退出后恢复原绑定。

    用于显式传递：调用方可以先 :meth:`Store.clone` / :func:`fork` 得到
    快照，再显式交给任意协程；被调用方对存储的修改不外泄。
    """
    token = _current.set(store.clone())
    try:
        return await coro_func(*args, **kwargs)
    finally:
        _current.reset(token)


def spawn(coro: Awaitable[Any], *, store: Optional[Store] = None) -> asyncio.Task:
    """派生子任务（隐式或显式传播），立即返回已排程的 Task。

    复制语义（在 *调用时刻* 同步完成，而非子任务启动时刻）：
      * ``store=None``（隐式传播）：深拷贝调用方当前 Store 的全部内容；
        无上下文时子任务拿到空 Store。
      * ``store=s``（显式传递）：深拷贝给定 Store ``s``，与调用方当前
        上下文无关。
    两种情况下子任务拿到的都是独立副本：子任务内 set/delete 以及对
    可变值的原地修改，均不会影响父任务或任何兄弟任务。
    """
    parent = _current.get() if store is None else store
    child_ctx = contextvars.copy_context()
    child_ctx.run(_current.set, Store() if parent is None else parent.clone())
    return asyncio.create_task(coro, context=child_ctx)


async def gather(*coros: Awaitable[Any]) -> list:
    """便捷封装：以隐式传播方式派生多个兄弟任务并收集结果。"""
    return await asyncio.gather(*(spawn(c) for c in coros))
