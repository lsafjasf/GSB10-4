"""逐层可见范围演示：实际观测值 vs 手工推算值（对拍数据）。

运行: python3 layer_demo.py
"""

import asyncio
import copy

import corolocal as cl


def reference_chain():
    """手工推算的参考链：每层 = 父层 dict 的 deepcopy，再做本层修改。"""
    root = {"a": 1, "obj": {"v": 0}, "level": "root"}
    child = copy.deepcopy(root)
    child.update(a=2, level="child"); child["obj"]["v"] = 20
    grand = copy.deepcopy(child)
    grand.update(a=3, level="grand"); grand["obj"]["v"] = 30
    sib = copy.deepcopy(root)
    sib.update(a=9, level="sibling")
    return {"root": root, "child": child, "grand": grand, "sibling": sib}


async def main():
    rows = []

    async def grandchild():
        rows.append(("grand", "进入时（=子层快照）",
                     (cl.get("a"), cl.require("obj")["v"], cl.get("level"))))
        cl.set("a", 3); cl.require("obj")["v"] = 30; cl.set("level", "grand")
        rows.append(("grand", "修改后",
                     (cl.get("a"), cl.require("obj")["v"], cl.get("level"))))

    async def child():
        rows.append(("child", "进入时（=根层快照）",
                     (cl.get("a"), cl.require("obj")["v"], cl.get("level"))))
        cl.set("a", 2); cl.require("obj")["v"] = 20; cl.set("level", "child")
        rows.append(("child", "修改后",
                     (cl.get("a"), cl.require("obj")["v"], cl.get("level"))))
        await cl.spawn(grandchild())
        rows.append(("child", "孙任务结束后（不得回灌）",
                     (cl.get("a"), cl.require("obj")["v"], cl.get("level"))))

    async def sibling():
        rows.append(("sibling", "进入时（=根层快照）",
                     (cl.get("a"), cl.require("obj")["v"], cl.get("level"))))
        cl.set("a", 9); cl.set("level", "sibling")
        rows.append(("sibling", "修改后",
                     (cl.get("a"), cl.require("obj")["v"], cl.get("level"))))

    cl.set("a", 1); cl.set("obj", {"v": 0}); cl.set("level", "root")
    rows.append(("root", "初始",
                 (cl.get("a"), cl.require("obj")["v"], cl.get("level"))))
    await asyncio.gather(cl.spawn(child()), cl.spawn(sibling()))
    rows.append(("root", "全部子任务结束后（不得回灌）",
                 (cl.get("a"), cl.require("obj")["v"], cl.get("level"))))

    ref = reference_chain()
    expected = {
        ("root", "初始"): (ref["root"]["a"], 0, "root"),
        ("child", "进入时（=根层快照）"): (ref["root"]["a"], 0, "root"),
        ("child", "修改后"): (2, 20, "child"),
        ("grand", "进入时（=子层快照）"): (2, 20, "child"),
        ("grand", "修改后"): (3, 30, "grand"),
        ("child", "孙任务结束后（不得回灌）"): (2, 20, "child"),
        ("sibling", "进入时（=根层快照）"): (ref["root"]["a"], 0, "root"),
        ("sibling", "修改后"): (9, 0, "sibling"),
        ("root", "全部子任务结束后（不得回灌）"): (1, 0, "root"),
    }

    print(f"{'层':<8}{'观测点':<32}{'实际 (a,obj.v,level)':<28}{'手工推算':<28}对拍")
    print("-" * 116)
    all_ok = True
    for layer, point, actual in rows:
        want = expected[(layer, point)]
        ok = tuple(actual) == want
        all_ok &= ok
        print(f"{layer:<8}{point:<32}{str(actual):<28}{str(want):<28}{'✓' if ok else '✗ MISMATCH'}")

    print("\n各层最终 Store 内容（参考模型）：")
    for name, d in ref.items():
        print(f"  {name:<8} {d}")
    print("\n对拍结果:", "全部一致 ✓" if all_ok else "存在不一致 ✗")
    assert all_ok, "layer-by-layer cross-check failed"


if __name__ == "__main__":
    asyncio.run(main())
