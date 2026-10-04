"""协程本地存储：派生时复制，不共享。

实现要点
--------
1. 每个 ``LocalStore`` 持有一张普通 dict，存储本体之间互不共享。
2. 当前 store 通过 ``contextvars.ContextVar`` 寻址。``asyncio.create_task``
   会在创建任务时复制调用方的 Context；子任务 runner 再把 ContextVar 指向
   一份**新复制出来的** LocalStore，因此子任务的任何 set/delete 都落在自己
   的 dict 上，不会串回父任务或兄弟任务。
3. 复制发生在 ``spawn()`` 被同步调用的那一刻（copy-before-first-await）。
   asyncio 是协作式调度，两次 await 之间不会切走任务，所以快照要么看到派
   生前的完整状态，要么看到派生后的完整状态，不存在读到“改了一半”的脏数
   据的可能。
4. 复制粒度：
   * ``deep=False``（默认，浅复制）：复制键绑定（dict.copy），值对象本身
     仍是同一个引用。替换绑定（set/delete）完全隔离；原地修改可变值对象
     （如 ``get("k").append(1)``）不在隔离范围内——与 Python 中普通 dict
     复制的语义一致。
   * ``deep=True``（深复制）：用 ``copy.deepcopy`` 递归复制值，连可变值的
     原地修改也隔离开，代价是更慢、值必须可深拷贝。
"""

from __future__ import annotations

import contextvars
import copy as _copy
from typing import Any, Awaitable, Callable, Mapping, Optional, TypeVar, Union

T = TypeVar("T")

_UNSET = object()

_current_store: contextvars.ContextVar[Optional["LocalStore"]] = contextvars.ContextVar(
    "tasklocal_current_store", default=None
)


class LocalStore:
    """一张任务私有的键值表。"""

    __slots__ = ("_data",)

    def __init__(self, data: Optional[Mapping[str, Any]] = None) -> None:
        # 构造时即复制入参，避免调用方后续改 dict 影响本表。
        self._data: dict[str, Any] = dict(data) if data else {}

    # ---- 基本读写 -------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def require(self, key: str) -> Any:
        """读取，键不存在时抛 KeyError（区别于“值就是 None”）。"""
        if key not in self._data:
            raise KeyError(key)
        return self._data[key]

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def keys(self):
        return self._data.keys()

    def to_dict(self, deep: bool = False) -> dict[str, Any]:
        """导出快照；deep=True 时递归复制值。"""
        return _copy.deepcopy(self._data) if deep else dict(self._data)

    # ---- 复制 -----------------------------------------------------------
    def copy(self, deep: bool = False) -> "LocalStore":
        return LocalStore(_copy.deepcopy(self._data) if deep else self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __eq__(self, other: object) -> bool:
        return isinstance(other, LocalStore) and self._data == other._data

    def __repr__(self) -> str:
        return f"LocalStore({self._data!r})"


# ---------------------------------------------------------------------------
# 模块级便捷 API：隐式操作“当前任务”的 store
# ---------------------------------------------------------------------------

def current_store() -> Optional[LocalStore]:
    """返回当前任务的 store；从未通过 spawn/bind 激活时返回 None。"""
    return _current_store.get()


def get(key: str, default: Any = None) -> Any:
    """无上下文（未激活 store）时确定性地返回 default，不抛错。"""
    store = _current_store.get()
    if store is None:
        return default
    return store.get(key, default)


def set(key: str, value: Any) -> None:
    """无上下文时确定性地抛 RuntimeError——绝不隐式创建共享存储。"""
    store = _current_store.get()
    if store is None:
        raise RuntimeError(
            "tasklocal.set() called outside of any spawn()/bind() context; "
            "use spawn(coro) or bind(LocalStore(...)) first"
        )
    store.set(key, value)


def delete(key: str) -> None:
    store = _current_store.get()
    if store is not None:
        store.delete(key)


class _Bind:
    def __init__(self, store: LocalStore) -> None:
        self._store = store
        self._token: Optional[contextvars.Token[Optional[LocalStore]]] = None

    def __enter__(self) -> LocalStore:
        self._token = _current_store.set(self._store)
        return self._store

    def __exit__(self, *exc: object) -> None:
        assert self._token is not None
        _current_store.reset(self._token)


def bind(store: LocalStore) -> _Bind:
    """在当前任务内临时激活一个 store（同步或 async with 均可）。"""
    return _Bind(store)


# ---------------------------------------------------------------------------
# 派生
# ---------------------------------------------------------------------------

CoroLike = Union[Awaitable[Any], Callable[[], Awaitable[Any]]]


def spawn(
    coro: CoroLike,
    *,
    context: Optional[Mapping[str, Any]] = None,
    deep: bool = False,
    name: Optional[str] = None,
) -> Any:
    """派生子任务。

    参数
    ----
    coro:
        协程对象 ``child()`` 或零参协程工厂。传工厂时，协程在子任务私有
        store 绑定之后才创建，适合需要在创建期就读取上下文的场景。
    context:
        - None（隐式传播）：在**当前调用点**快照当前 store，复制给子任务；
          当前没有 store 时，子任务拿到一个空 store。
        - Mapping（显式传递）：忽略当前 store，子任务直接以该映射的副本为
          初始存储。用于跨层裁剪/伪造上下文，或在非 async 入口注入数据。
    deep:
        True 时对值做 copy.deepcopy（隔离可变值的原地修改），默认浅复制。

    返回
    ----
    asyncio.Task（由 asyncio.create_task 调度）。
    """
    # —— 关键：在任何 await 之前完成快照，保证确定性 ——
    if context is None:
        parent = _current_store.get()
        initial = _copy.deepcopy(parent._data) if (deep and parent is not None) else (
            dict(parent._data) if parent is not None else {}
        )
        origin = "inherited"
    else:
        initial = _copy.deepcopy(dict(context)) if deep else dict(context)
        origin = "explicit"

    async def _runner() -> Any:
        # create_task 已复制 Context；这里只替换当前任务副本里的 ContextVar，
        # 父任务与兄弟任务看到的仍是各自的 LocalStore。
        child_store = LocalStore(initial)
        token = _current_store.set(child_store)
        try:
            if callable(coro):
                return await coro()
            return await coro
        finally:
            _current_store.reset(token)

    task = _create_task(_runner(), name=name)
    # 供测试/调试观察传播来源
    task.tasklocal_origin = origin  # type: ignore[attr-defined]
    return task


def _create_task(coro: Awaitable[Any], name: Optional[str]):
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        raise RuntimeError("tasklocal.spawn() must be called inside a running event loop")
    return loop.create_task(coro, name=name)
