"""对拍演示：打印多层嵌套与兄弟并发修改的手工推算值 vs 实际运行值。

运行：python3 demo.py
"""

import asyncio

from tasklocal import LocalStore, bind, get, set, spawn


async def multi_level():
    rows = []

    def snapshot(layer, expected):
        actual = {k: get(k) for k in ("a", "b", "c") if k in (current_keys())}
        rows.append((layer, expected, actual, expected == actual))

    def current_keys():
        return [k for k in ("a", "b", "c") if get(k) is not None]

    async def l3():
        set("c", 3)
        snapshot("L3 内部", {"a": 100, "b": 2, "c": 3})

    async def l2():
        set("a", 100)
        snapshot("L2 内部", {"a": 100, "b": 2})
        await spawn(l3())
        snapshot("L2 返回后", {"a": 100, "b": 2})

    async def l1():
        set("a", 10)
        set("b", 2)
        snapshot("L1 内部", {"a": 10, "b": 2})
        await spawn(l2())
        snapshot("L1 返回后", {"a": 10, "b": 2})

    set("a", 1)
    snapshot("L0 派生前", {"a": 1})
    await spawn(l1())
    snapshot("L0 子任务返回后", {"a": 1})

    print("== 多层嵌套逐层对拍 ==")
    print(f"{'层级':14s} {'手工推算':30s} {'实际运行':30s} 一致")
    for layer, exp, act, ok in rows:
        print(f"{layer:14s} {exp!s:30s} {act!s:30s} {'✓' if ok else '✗'}")
    assert all(ok for _, _, _, ok in rows)


async def siblings():
    set("x", 0)
    ev_a, ev_b = asyncio.Event(), asyncio.Event()

    async def a():
        set("x", 111)
        ev_a.set()
        await ev_b.wait()       # 强制交错：B 已写完 x=222
        return get("x")

    async def b():
        set("x", 222)
        ev_b.set()
        await ev_a.wait()
        return get("x")

    ra, rb = await asyncio.gather(spawn(a()), spawn(b()))
    print("\n== 兄弟任务并发修改同一 key ==")
    print(f"{'角色':16s} {'手工推算':>8s} {'实际运行':>8s} 一致")
    for name, exp, act in [("兄弟A 读回", 111, ra), ("兄弟B 读回", 222, rb),
                           ("父任务读回", 0, get("x"))]:
        print(f"{name:16s} {exp:>8} {act:>8} {'✓' if exp == act else '✗'}")
    assert (ra, rb, get("x")) == (111, 222, 0)


async def explicit_vs_implicit():
    set("a", 1)
    set("secret", "parent")

    async def child():
        return get("a"), get("secret", "<absent>")

    implicit = await spawn(child())
    explicit = await spawn(child(), context={"a": 999})
    print("\n== 显式传递 vs 隐式传播 ==")
    print(f"隐式 spawn(child())                       -> (a,secret) = {implicit}")
    print(f"显式 spawn(child(), context={{'a':999}})    -> (a,secret) = {explicit}")
    assert implicit == (1, "parent")
    assert explicit == (999, "<absent>")


async def main():
    with bind(LocalStore()):
        await multi_level()
        await siblings()
        await explicit_vs_implicit()
    print("\n全部对拍通过。")


if __name__ == "__main__":
    asyncio.run(main())
