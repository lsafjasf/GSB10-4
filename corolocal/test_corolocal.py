"""corolocal 自测（仅标准库 unittest）。

覆盖：无上下文、单层派生、多层派生、兄弟任务并发修改、显式 vs 隐式
传播、快照时机确定性、以及与手工参考模型（纯 deepcopy 链）逐层对拍。
"""

import asyncio
import copy
import unittest

import corolocal as cl


def run(coro):
    return asyncio.run(coro)


class TestNoContext(unittest.TestCase):
    """边界：当前任务从未初始化过 Store。"""

    def test_get_returns_default_deterministically(self):
        self.assertIsNone(cl.get("x"))
        self.assertEqual(cl.get("x", 42), 42)
        self.assertFalse(cl.has("x"))
        self.assertIsNone(cl.current_store())

    def test_require_and_delete_raise_keyerror(self):
        with self.assertRaises(KeyError):
            cl.require("x")
        with self.assertRaises(KeyError):
            cl.delete("x")

    def test_fork_without_context_is_empty_store(self):
        s = cl.fork()
        self.assertEqual(s.as_dict(), {})

    def test_set_without_context_binds_root_store(self):
        run(self._async_set_root())

    async def _async_set_root(self):
        cl.set("k", "v")
        self.assertEqual(cl.get("k"), "v")
        # 派生出的子任务能读到（复制），但改了不串回来
        async def child():
            self.assertEqual(cl.get("k"), "v")
            cl.set("k", "child")
            return cl.get("k")

        result = await cl.spawn(child())
        self.assertEqual(result, "child")
        self.assertEqual(cl.get("k"), "v")


class TestSingleSpawn(unittest.TestCase):
    """单层派生：基本值与可变值都必须隔离。"""

    def test_scalar_isolation(self):
        async def scenario():
            cl.set("a", 1)
            async def child():
                self.assertEqual(cl.get("a"), 1)
                cl.set("a", 2)
                cl.set("b", 3)
                return cl.get("a")
            self.assertEqual(await cl.spawn(child()), 2)
            # 父任务看不到子任务的任何修改
            self.assertEqual(cl.get("a"), 1)
            self.assertFalse(cl.has("b"))
        run(scenario())

    def test_mutable_value_deep_copied(self):
        async def scenario():
            cl.set("obj", {"nested": [1, 2, 3], "s": {"x": 10}})
            parent_obj = cl.require("obj")
            async def child():
                obj = cl.require("obj")
                obj["nested"].append(99)        # 原地改列表
                obj["s"]["x"] = 999            # 原地改嵌套 dict
                obj["new"] = "leak?"
                return obj
            child_obj = await cl.spawn(child())
            self.assertEqual(child_obj["nested"][-1], 99)
            self.assertEqual(child_obj["s"]["x"], 999)
            # 父任务持有的引用与父 Store 中的值都未被污染
            self.assertEqual(parent_obj, {"nested": [1, 2, 3], "s": {"x": 10}})
            self.assertEqual(cl.require("obj"),
                             {"nested": [1, 2, 3], "s": {"x": 10}})
            self.assertIs(cl.require("obj"), parent_obj)
        run(scenario())

    def test_child_starts_from_call_time_snapshot(self):
        """快照在 spawn() 调用时刻同步拍定，即使子任务稍后才运行。"""
        async def scenario():
            cl.set("a", 1)
            async def child():
                return cl.get("a")
            task = cl.spawn(child())     # 已排程但尚未运行
            cl.set("a", 100)            # 父任务随后修改
            self.assertEqual(await task, 1)  # 子任务读到的是旧快照
            self.assertEqual(cl.get("a"), 100)
        run(scenario())


class TestSiblingsConcurrent(unittest.TestCase):
    """兄弟任务并发交错修改：互不影响，父任务也不受影响。"""

    def test_siblings_isolated(self):
        async def scenario():
            cl.set("counter", 0)
            cl.set("log", [])

            async def sibling(tag, rounds):
                seen = []
                for i in range(rounds):
                    cl.set("counter", cl.get("counter") + 1)
                    cl.require("log").append(f"{tag}{i}")
                    await asyncio.sleep(0)   # 主动让出，制造交错
                    seen.append(cl.get("counter"))
                return {"counter": cl.get("counter"),
                        "log": cl.get("log"), "seen": seen}

            t1 = cl.spawn(sibling("a", 4))
            t2 = cl.spawn(sibling("b", 4))
            t3 = cl.spawn(sibling("c", 2))   # 稍后派生，仍只看到父快照
            r1, r2, r3 = await asyncio.gather(t1, t2, t3)

            # 每个兄弟各自从 0 起独立计数，日志互不串扰
            self.assertEqual(r1["counter"], 4)
            self.assertEqual(r2["counter"], 4)
            self.assertEqual(r3["counter"], 2)
            self.assertEqual(r1["log"], [f"a{i}" for i in range(4)])
            self.assertEqual(r2["log"], [f"b{i}" for i in range(4)])
            self.assertEqual(r3["log"], [f"c{i}" for i in range(2)])
            # 父任务从未被修改：无脏读
            self.assertEqual(cl.get("counter"), 0)
            self.assertEqual(cl.get("log"), [])
        run(scenario())


class TestMultiLayer(unittest.TestCase):
    """多层派生逐层可见范围 + 与手工参考模型对拍。"""

    @staticmethod
    def _reference_model():
        """手工推算：每一层都是上一层 dict 的 deepcopy。"""
        root = {"a": 1, "obj": {"v": 0}, "level": "root"}
        child = copy.deepcopy(root)
        child["a"] = 2; child["obj"]["v"] = 20; child["level"] = "child"
        grand = copy.deepcopy(child)
        grand["a"] = 3; grand["obj"]["v"] = 30; grand["level"] = "grand"
        sibling = copy.deepcopy(root)
        sibling["a"] = 9; sibling["level"] = "sibling"
        return root, child, grand, sibling

    def test_layer_by_layer_matches_reference(self):
        observed = {}

        async def grandchild():
            observed["grand_in"] = cl.get("a"), cl.require("obj")["v"], cl.get("level")
            cl.set("a", 3)
            cl.require("obj")["v"] = 30
            cl.set("level", "grand")
            observed["grand_out"] = cl.get("a"), cl.require("obj")["v"], cl.get("level")
            return cl.get("a")

        async def child():
            observed["child_in"] = cl.get("a"), cl.require("obj")["v"], cl.get("level")
            cl.set("a", 2)
            cl.require("obj")["v"] = 20
            cl.set("level", "child")
            await cl.spawn(grandchild())
            # 孙任务的修改不得回灌到子任务
            observed["child_after_grand"] = (cl.get("a"), cl.require("obj")["v"],
                                             cl.get("level"))

        async def sibling():
            observed["sib_in"] = cl.get("a"), cl.require("obj")["v"], cl.get("level")
            cl.set("a", 9)
            cl.set("level", "sibling")
            observed["sib_out"] = cl.get("a"), cl.require("obj")["v"], cl.get("level")

        async def root():
            cl.set("a", 1)
            cl.set("obj", {"v": 0})
            cl.set("level", "root")
            t1 = cl.spawn(child())
            t2 = cl.spawn(sibling())
            await asyncio.gather(t1, t2)
            observed["root_after"] = cl.get("a"), cl.require("obj")["v"], cl.get("level")

        run(root())

        root, child, grand, sibling = self._reference_model()

        # 逐层对拍：进入层时看到的内容 = 手工参考模型上一层
        self.assertEqual(observed["child_in"], (root["a"], root["obj"]["v"], "root"))
        self.assertEqual(observed["sib_in"], (root["a"], root["obj"]["v"], "root"))
        self.assertEqual(observed["grand_in"], (child["a"], child["obj"]["v"], "child"))
        # 层内修改后只在该层及其后代可见
        self.assertEqual(observed["grand_out"], (grand["a"], grand["obj"]["v"], "grand"))
        self.assertEqual(observed["child_after_grand"],
                         (child["a"], child["obj"]["v"], "child"))
        self.assertEqual(observed["sib_out"],
                         (sibling["a"], 0, "sibling"))
        # 根层始终是原始值
        self.assertEqual(observed["root_after"],
                         (root["a"], root["obj"]["v"], "root"))


class TestExplicitVsImplicit(unittest.TestCase):
    """显式传递（store= / use_store）与隐式传播的差异。"""

    def test_explicit_store_overrides_context(self):
        async def scenario():
            cl.set("k", "implicit")
            custom = cl.Store()
            custom.set("k", "explicit")
            async def child():
                return cl.get("k")
            implicit_task = cl.spawn(child())
            explicit_task = cl.spawn(child(), store=custom)
            self.assertEqual(await implicit_task, "implicit")
            self.assertEqual(await explicit_task, "explicit")
            # 显式 Store 也被克隆：子任务修改不影响源 Store
            self.assertEqual(custom.get("k"), "explicit")
        run(scenario())

    def test_use_store_scopes_binding_and_restores(self):
        async def scenario():
            cl.set("k", "outer")
            snapshot = cl.fork()          # 显式快照
            snapshot.set("k", "snap")

            async def work():
                self.assertEqual(cl.get("k"), "snap")
                cl.set("k", "mutated")
            await cl.use_store(snapshot, work)
            # 退出后恢复外层绑定；源快照与外层均未被修改
            self.assertEqual(cl.get("k"), "outer")
            self.assertEqual(snapshot.get("k"), "snap")
        run(scenario())

    def test_explicit_empty_store_hides_parent(self):
        async def scenario():
            cl.set("secret", "visible-implicitly")

            async def child():
                return cl.has("secret")
            self.assertIs(await cl.spawn(child(), store=cl.Store()), False)
            self.assertIs(await cl.spawn(child()), True)
        run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
