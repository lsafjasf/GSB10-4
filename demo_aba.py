"""ABA 的确定性时序实证（单线程脚本化 + 真实双线程模拟慢读者）。

运行：python3 demo_aba.py

三个场景使用同一条经典交错序列：
  1. 读者 T1 读到栈头 A（next=B），随后被挂起；
  2. T2 连续弹出 A、B，再把 A/B 复用重新入栈；
  3. T1 恢复执行 CAS。

场景一：无版本号 + 立即复用        -> CAS 误判成功，元素丢失（栈被搞坏）
场景二：版本号 mod2 + 提前回收复用 -> 回绕后版本号撞回，CAS 仍误判成功
场景三：版本号 mod2 + 纪元延迟回收 -> 慢读者 pin 住时节点无法被回收复用，
                                      CAS 失败，重试后结果正确
"""

import threading

from aba_stack import EMPTY, Head, LockFreeStack, UnsafeNoTagStack


def drain(stack):
    out = []
    while True:
        v = stack.pop()
        if v is EMPTY:
            return out
        out.append(v)


def scenario_1_unsafe_no_tag():
    print("=" * 70)
    print("场景一：无版本号 + 节点立即复用（无任何防护）")
    print("-" * 70)
    s = UnsafeNoTagStack()
    s.push("b")
    s.push("a")
    # T1 开始一次 pop：读到 head=A, A.next=B，然后挂起
    stale_head = s._head.load()       # A("a")
    stale_next = stale_head.next      # B("b")
    print("T1 读旧栈头 A='a', A.next=B='b'，挂起")

    # T2：弹空，再用回收节点压入 x、y
    print("T2 弹出 %r、%r" % (s.pop(), s.pop()))
    s.push("x")   # 复用 B
    s.push("y")   # 复用 A -> 栈头又变回 A 这个对象
    print("T2 压入 'x'(复用B)、'y'(复用A)，栈头对象重新变成 A")

    # T1 恢复，执行它读到的 CAS：A -> B
    ok = s._head.compare_and_swap(stale_head, stale_next)
    print("T1 恢复后裸指针 CAS(A -> B) 结果: %s" % ok)
    remaining = drain(s)
    print("实际残留栈内容: %r" % remaining)
    # 共推入 a,b,x,y；弹出 a,b；本应残留 y,x；实际只剩 x —— 'y' 丢失
    assert ok is True
    assert remaining == ["x"], remaining
    print(">>> 结论：CAS 误判成功，'y' 被静默丢弃，元素守恒被破坏！")


def scenario_2_tagged_but_premature_free(modulus=2):
    print("=" * 70)
    print("场景二：版本号 mod %d（强制回绕）+ 读者未公告 + 提前回收复用" % modulus)
    print("-" * 70)
    s = LockFreeStack(tag_modulus=modulus)
    s.push("b")
    s.push("a")
    # T1 读到 (A, tag=0) 并挂起，且没有 pin（等价于 C 里裸读指针、未登记 hazard）
    stale_head = s._head.load()  # Head(A, 0)
    stale_next = stale_head.node.next
    print("T1 读旧栈头 Head(A, tag=0)，挂起（未 pin）")

    print("T2 弹出 %r、%r" % (s.pop(), s.pop()))
    for _ in range(4):
        s.reclaimer.reclaim_now()  # T1 未 pin -> 纪元顺利前进，A/B 提前回收
    print("提前回收: reclaimed=%d，A/B 已进空闲链表" % s.reclaimer.reclaimed)
    s.push("x")  # 复用 B
    s.push("y")  # 复用 A；mod 2 下 tag 恰好撞回 0
    cur = s._head.load()
    print("T2 压入后栈头: Head(tag=%d, node is A=%s)" %
          (cur.tag, cur.node is stale_head.node))

    ok = s._head.compare_and_swap(stale_head, Head(stale_next, 1))
    print("T1 的过期 CAS Head(A,0) -> Head(B,1) 结果: %s" % ok)
    remaining = drain(s)
    print("实际残留栈内容: %r" % remaining)
    assert ok is True
    assert cur == stale_head  # 回绕 + 复用使二元组完全相等
    assert remaining == ["x"], remaining
    print(">>> 结论：仅有版本号、但版本号回绕且节点在读者持引期间被提前")
    print("    回收复用，ABA 依旧发生 —— 这就是延迟回收必须安全的论证。")


def scenario_3_tagged_plus_epoch_reclaim(modulus=2):
    print("=" * 70)
    print("场景三：版本号 mod %d（强制回绕）+ 纪元延迟回收（完整防护）" % modulus)
    print("-" * 70)
    s = LockFreeStack(tag_modulus=modulus)
    s.push("b")
    s.push("a")
    pinned = threading.Event()
    release = threading.Event()
    captured = {}

    def reader_t1():
        s.reclaimer.pin()  # 真实 pop() 进入临界区时会 pin
        head = s._head.load()
        captured["head"] = head
        captured["next"] = head.node.next
        pinned.set()
        release.wait(5)
        # 恢复后重试整次 pop，正是 LockFreeStack.pop 的 CAS 失败重试路径
        s.reclaimer.unpin()

    t1 = threading.Thread(target=reader_t1)
    t1.start()
    pinned.wait(5)
    stale_head = captured["head"]
    print("T1 pin 并读旧栈头 Head(A, tag=0)，挂起")

    # T2 线程完成弹出 + 回收 + 重新压入
    def worker_t2():
        s.pop()
        s.pop()
        for _ in range(4):
            s.reclaimer.reclaim_now()  # 纪元被 T1 的公告卡住，A/B 不会回收
        s.push("x")
        s.push("y")

    t2 = threading.Thread(target=worker_t2)
    t2.start()
    t2.join()
    print("T2 操作完: reclaimed=%d (T1 pin 住，必须为 0)" % s.reclaimer.reclaimed)
    assert s.reclaimer.reclaimed == 0

    release.set()
    t1.join()
    # T1 持过期二元组直接做 CAS —— 必须失败（栈头节点已不是 A）
    ok = s._head.compare_and_swap(stale_head, Head(captured["next"], 1))
    print("T1 的过期 CAS Head(A,0) -> Head(B,1) 结果: %s" % ok)
    assert ok is False
    remaining = drain(s)
    print("T1 重试 pop 后实际残留栈内容: %r" % remaining)
    assert remaining == ["y", "x"], remaining
    print(">>> 结论：pin 期间节点无法被回收复用，栈头二元组不可能与过期快照")
    print("    相等；CAS 失败并安全重试，元素守恒（y, x 均在）。")


if __name__ == "__main__":
    scenario_1_unsafe_no_tag()
    scenario_2_tagged_but_premature_free(modulus=2)
    scenario_3_tagged_plus_epoch_reclaim(modulus=2)
    print("=" * 70)
    print("全部场景符合预期：无防护必坏；提前释放+回绕必坏；完整防护安全。")
