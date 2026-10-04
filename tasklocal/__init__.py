"""协程本地存储（Coroutine-Local Storage），派生时按复制语义传播。

公开 API：
    get(key, default=None)    读取当前任务的本地存储（无上下文时返回 default）
    set(key, value)           写入当前任务的本地存储（无上下文时报错）
    delete(key)               删除当前任务本地存储中的绑定
    current_store()           取得当前 LocalStore（未激活时为 None）
    spawn(coro_factory, ...)  派生子任务，复制当前存储或使用显式传入的存储
    bind(store)               在当前任务内临时激活一个 store（上下文管理器）
    LocalStore                存储本体，可手工构造、快照、复制
"""

from .store import (
    LocalStore,
    bind,
    current_store,
    delete,
    get,
    set,
    spawn,
)

__all__ = [
    "LocalStore",
    "bind",
    "current_store",
    "delete",
    "get",
    "set",
    "spawn",
]
