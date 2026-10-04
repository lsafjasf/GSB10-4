"""tasklocal 自测：无上下文 / 单层派生 / 多层派生 / 兄弟并发修改 / 边界行为。

对拍方法：每个用例的期望值都由手工推算（注释中给出推导），
运行时用 assertEqual 与实际值对拍；多层派生用例额外打印逐层对照表。
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tasklocal import LocalStore, bind, current_store, delete, get, set, spawn



class RootStoreCase(unittest.IsolatedAsyncioTestCase):
    """为“父任务”预置一个根 store 的基类（模拟应用入口的 bind）。"""

    async def asyncSetUp(self):
        self._root_bind = bind(LocalStore())
        self._root_bind.__enter__()

    async def asyncTearDown(self):
        self._root_bind.__exit__(None, None, None)

class TestNoContext(unittest.IsolatedAsyncioTestCase):
    """情形 1：未设置任何上下文。"""

    async def test_get_returns_default(self):
        # 手工推算：无 store -> get 一律回退 default
        self.assertIsNone(get("missing"))
        self.assertEqual(get("missing", 42), 42)
        self.assertIsNone(current_store())

    async def test_set_raises_deterministically(self):
        # 手工推算：无 store -> set 抛 RuntimeError（绝不隐式建共享存储）
        with self.assertRaises(RuntimeError):
            set("k", 1)

    async def test_delete_is_noop(self):
        delete("k")  # 不抛错即通过

    async def test_spawn_with_no_parent_store_gives_empty_store(self):
        async def child():
            return get("k", "empty"), current_store() is not None

        result, has_store = await spawn(child())
        self.assertEqual(result, "empty")
        self.assertTrue(has_store)  # 子任务内有自己的（空）store，可写


class TestSingleLevelSpawn(RootStoreCase):
    """情形 2：单层派生，子任务修改不得影响父任务。"""

    async def test_child_writes_do_not_leak_to_parent(self):
        set("a", 1)  # 父任务：{a: 1}

        async def child():
            seen_on_entry = get("a")      # 期望 1（继承快照）
            set("a", 10)                  # 只写自己的副本
            set("b", 2)
            return seen_on_entry, get("a"), get("b")

        seen, child_a, child_b = await spawn(child())
        # 对拍：子任务入口看到父值 1；子任务内 a=10, b=2
        self.assertEqual((seen, child_a, child_b), (1, 10, 2))
        # 对拍：父任务仍是 {a: 1}，看不到 b
        self.assertEqual(get("a"), 1)
        self.assertIsNone(get("b"))

    async def test_parent_mutation_after_spawn_not_visible_to_child(self):
        # 快照在 spawn() 同步调用点完成
        set("x", 1)
        async def child():
            return get("x")
        task = spawn(child())   # 此刻快照 x=1
        set("x", 2)             # 父任务随后修改
        self.assertEqual(await task, 1)   # 子任务看到快照值 1
        self.assertEqual(get("x"), 2)     # 父任务是自己的 2


class TestMultiLevelSpawn(RootStoreCase):
    """情形 3：四层嵌套派生，逐层可见范围与手工推算对拍。

    手工推算（浅复制、set 只写本层副本）：

        L0 root : {a:1}
        L1      : 继承 {a:1}，set a=10, b=2      -> {a:10, b:2}
        L2      : 继承 {a:10, b:2}，set a=100    -> {a:100, b:2}
        L3      : 继承 {a:100, b:2}，set c=3     -> {a:100, b:2, c:3}
        回到 L2 : {a:100, b:2}（看不到 c）
        回到 L1 : {a:10, b:2} （看不到 a=100、c）
        回到 L0 : {a:1}       （看不到 b、c，a 未被改）
    """

    EXPECTED = {
        "L0_before": {"a": 1},
        "L1": {"a": 10, "b": 2},
        "L2": {"a": 100, "b": 2},
        "L3": {"a": 100, "b": 2, "c": 3},
        "L2_after": {"a": 100, "b": 2},
        "L1_after": {"a": 10, "b": 2},
        "L0_after": {"a": 1},
    }

    async def test_four_levels(self):
        actual = {}

        def view():
            return {k: get(k) for k in ("a", "b", "c") if get(k) is not None}

        async def l3():
            set("c", 3)
            actual["L3"] = view()

        async def l2():
            set("a", 100)
            actual["L2"] = view()
            await spawn(l3())
            actual["L2_after"] = view()

        async def l1():
            set("a", 10)
            set("b", 2)
            actual["L1"] = view()
            await spawn(l2())
            actual["L1_after"] = view()

        set("a", 1)
        actual["L0_before"] = view()
        await spawn(l1())
        actual["L0_after"] = view()

        # 打印逐层对拍表（-v 运行时可见）
        print("\n逐层对拍（expected == actual 全部成立）：")
        for layer in self.EXPECTED:
            print(f"  {layer:10s} expected={self.EXPECTED[layer]!r:28s} actual={actual[layer]!r}")
        self.assertEqual(actual, self.EXPECTED)


class TestSiblingConcurrency(RootStoreCase):
    """情形 4：兄弟任务并发修改同一个 key，互不可见。"""

    async def test_siblings_isolated(self):
        set("x", 0)
        ev_a_written = asyncio.Event()
        ev_b_written = asyncio.Event()

        async def sib_a():
            set("x", 111)
            ev_a_written.set()
            await ev_b_written.wait()   # 等兄弟也写完，强制交错
            await asyncio.sleep(0)
            return get("x")

        async def sib_b():
            set("x", 222)
            ev_b_written.set()
            await ev_a_written.wait()
            await asyncio.sleep(0)
            return get("x")

        ra, rb = await asyncio.gather(spawn(sib_a()), spawn(sib_b()))
        # 对拍：交错执行后各自仍读到自己的写入
        self.assertEqual((ra, rb), (111, 222))
        self.assertEqual(get("x"), 0)  # 父任务未被污染

    async def test_many_siblings_storm(self):
        # 200 个兄弟同时写同一 key，每个都必须读回自己的值
        set("k", -1)
        n = 200

        async def worker(i):
            set("k", i)
            await asyncio.sleep(0)      # 让出调度，制造交错
            await asyncio.sleep(0)
            return get("k")

        results = await asyncio.gather(*(spawn(worker(i)) for i in range(n)))
        self.assertEqual(results, list(range(n)))
        self.assertEqual(get("k"), -1)


class TestExplicitContext(RootStoreCase):
    """显式传递：忽略当前 store，以传入映射的副本为初始存储。"""

    async def test_explicit_overrides_parent(self):
        set("a", 1)
        set("secret", "parent-only")

        async def child():
            return get("a"), get("secret", "absent")

        a, secret = await spawn(child(), context={"a": 999})
        # 对拍：显式上下文完全替换隐式继承
        self.assertEqual((a, secret), (999, "absent"))
        self.assertEqual(get("a"), 1)

    async def test_explicit_mapping_copied_at_spawn(self):
        src = {"v": 1}
        async def child():
            return get("v")
        task = spawn(child(), context=src)
        src["v"] = 2            # spawn 之后改源映射
        self.assertEqual(await task, 1)   # 子任务不受源映射后续修改影响

    async def test_child_write_does_not_leak_into_source_mapping(self):
        src = {"v": 1}
        async def child():
            set("v", 100)
            set("extra", True)
        await spawn(child(), context=src)
        self.assertEqual(src, {"v": 1})


class TestCopySemantics(RootStoreCase):
    """浅复制 vs 深复制的边界（README 中语义说明的对应用例）。"""

    async def test_shallow_copy_isolates_rebinding(self):
        set("n", 1)
        async def child():
            set("n", 2)
        await spawn(child())
        self.assertEqual(get("n"), 1)

    async def test_shallow_copy_shares_mutable_value_objects(self):
        # 浅复制只复制键绑定：原地修改可变值对象是共享的（文档化行为）
        set("lst", [1])
        async def child():
            get("lst").append(2)
        await spawn(child())
        self.assertEqual(get("lst"), [1, 2])   # 对拍：原地修改可见

    async def test_deep_copy_isolates_mutable_values(self):
        set("lst", [1])
        async def child():
            get("lst").append(2)
            return get("lst")
        child_lst = await spawn(child(), deep=True)
        self.assertEqual(child_lst, [1, 2])    # 子任务内是自己的副本
        self.assertEqual(get("lst"), [1])      # 父任务完全不受影响


class TestDeterminism(RootStoreCase):
    """并发修改下的确定行为：快照原子、无脏数据。"""

    async def test_snapshot_is_atomic_under_mutation_storm(self):
        # 父任务在派生间隙持续改 key；每个子任务看到的必须是某个完整整数状态，
        # 绝不可能是 None / 半更新状态（asyncio 协作式调度保证快照原子）。
        set("counter", 0)
        seen = []

        async def child():
            seen.append(get("counter"))

        tasks = []
        for i in range(1, 501):
            set("counter", i)          # 同步段内修改
            tasks.append(spawn(child()))  # 同步段内快照 -> 必为 i
        await asyncio.gather(*tasks)
        # 对拍：第 i 次派生的子任务必须恰好看到 i
        self.assertEqual(seen, list(range(1, 501)))

    async def test_no_shared_state_between_runs(self):
        async def child():
            set("tmp", "x")
            return sorted(current_store().keys())
        keys = await spawn(child())
        self.assertEqual(keys, ["tmp"])
        # 兄弟任务看不到上一个兄弟的写入
        keys2 = await spawn(child())
        self.assertEqual(keys2, ["tmp"])


class TestBindAndFactory(RootStoreCase):
    async def test_bind_activates_store_in_current_task(self):
        store = LocalStore({"u": "alice"})
        with bind(store):
            self.assertEqual(get("u"), "alice")
            set("u", "bob")
        self.assertIsNot(current_store(), store)  # 退出后恢复外层（根）store
        self.assertIsNone(get("u"))               # 根 store 里没有 u
        self.assertEqual(store.get("u"), "bob")   # 写入落在被 bind 的 store 上

    async def test_spawn_accepts_coroutine_factory(self):
        set("who", "parent")
        def factory():
            # 工厂在子任务 store 绑定之后才被调用
            who_at_create = get("who")
            async def inner():
                return who_at_create, get("who")
            return inner()
        self.assertEqual(await spawn(factory), ("parent", "parent"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
