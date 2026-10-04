"""无锁 Treiber 栈 + ABA 防护（Python 3，仅标准库）。

防护方案（两者结合，纵深防御）：

1. 标记指针（tagged pointer / 版本号）：
   栈头是 (node, tag) 的原子二元组，每次 CAS 成功都让 tag 加 1。
   CAS 比较的是「节点身份 + 版本号」而不是裸指针。节点被弹出后
   再次出现在栈头时版本号已经不同，过期的 CAS 必然失败。

2. 纪元式延迟回收（epoch-based reclamation）：
   弹出的节点不立即复用，先进入 limbo（延迟队列），只有当全局纪元
   相对退役纪元推进了两个纪元（retire_epoch + 2）后才真正回收进
   空闲链表。这保证任何线程临界区内可能引用的节点都不会被复用。

版本号回绕处理：
   生产配置 tag 用 Python 任意精度整数，永不回绕。tag_modulus 仅用于
   在测试/演示中模拟定宽（如 2 bit / 8 bit）回绕。定宽实现（如 64 位
   计数器）下的回绕安全性论证见 README，要点是：回绕导致误判要求
   「同一节点 + 同一版本号」在读者持有引用期间重现，而延迟回收已经
   保证持有期间节点不可能被回收复用。

关于"锁"：CPython 不向用户态暴露 CAS 原语，AtomicRef 用一把私有互斥
锁模拟双字原子比较交换（对应硬件的 cmpxchg16b）。锁之上的算法是货真
价实的无锁 Treiber 构造，竞态、版本号、延迟回收逻辑与 C/Java 实现一一
对应。
"""

from __future__ import annotations

import threading


EMPTY = object()  # 空栈时 pop() 返回的哨兵


class Node:
    __slots__ = ("value", "next")

    def __init__(self, value=None, nxt=None):
        self.value = value
        self.next = nxt


class Head:
    """(node, tag) 二元组，作为一个原子单位被 CAS。

    相等判定是「节点身份（is）+ 版本号相等」，与双字 CAS 语义一致。
    """

    __slots__ = ("node", "tag")

    def __init__(self, node, tag):
        self.node = node
        self.tag = tag

    def __eq__(self, other):
        return (
            isinstance(other, Head)
            and self.node is other.node
            and self.tag == other.tag
        )

    def __repr__(self):
        return "Head(node=%r, tag=%r)" % (self.node, self.tag)


class AtomicRef:
    """原子比较交换单元（硬件双字 CAS 的 Python 替身）。"""

    __slots__ = ("_value", "_lock")

    def __init__(self, value):
        self._value = value
        self._lock = threading.Lock()

    def load(self):
        return self._value

    def compare_and_swap(self, expected, new):
        with self._lock:
            if self._value == expected:
                self._value = new
                return True
            return False


class EpochReclaimer:
    """三纪元延迟回收 + 回收节点复用空闲链表。

    纪元推进不变式：只有当不存在任何在「早于当前纪元」处登记的线程
    （pin 住的线程）时，全局纪元才允许 E -> E+1。

    释放判定：在纪元 e 退役的节点，当全局纪元达到 e+2 时才回收。
    由不变式，所有在纪元 <= e 内进入临界区的线程此时都已离开，没有
    任何线程可能还持有该节点引用，回收/复用安全（证明见 README）。
    """

    SCAN_INTERVAL = 64  # 每退役多少个节点扫描一次 limbo

    def __init__(self):
        self._lock = threading.Lock()
        self._epoch = 0
        self._announced = {}  # 线程 ident -> pin 住的纪元；None 表示未临界
        self._limbo = {}  # 线程 ident -> [(retire_epoch, Node)]
        self._free = []  # 已真正回收、可复用的节点
        self._since_scan = 0
        self.retired = 0
        self.reclaimed = 0

    # ---- 临界区公告（hazard announcement）----

    def pin(self):
        """进入可能解引用节点的临界区：登记当前纪元。"""
        ident = threading.get_ident()
        with self._lock:
            self._announced[ident] = self._epoch

    def unpin(self):
        """离开临界区。"""
        ident = threading.get_ident()
        with self._lock:
            self._announced[ident] = None

    # ---- 节点生命周期 ----

    def alloc(self, value):
        """优先复用已安全回收的节点，否则新建。"""
        with self._lock:
            if self._free:
                node = self._free.pop()
                node.value = value
                node.next = None
                return node
        return Node(value, None)

    def retire(self, node):
        """节点退役：进入 limbo，暂不复用。"""
        ident = threading.get_ident()
        with self._lock:
            self._limbo.setdefault(ident, []).append((self._epoch, node))
            self.retired += 1
            self._since_scan += 1
            if self._since_scan >= self.SCAN_INTERVAL:
                self._since_scan = 0
                self._scan_locked()

    def reclaim_now(self):
        """立即尝试推进纪元并回收（基准/测试/演示用）。"""
        with self._lock:
            self._scan_locked()

    def _scan_locked(self):
        pinned = [e for e in self._announced.values() if e is not None]
        # 只有没有线程还停留在旧纪元时才允许纪元前进
        if not pinned or min(pinned) >= self._epoch:
            self._epoch += 1
        safe_epoch = self._epoch - 2
        for bag in self._limbo.values():
            kept = []
            for retire_epoch, node in bag:
                if retire_epoch <= safe_epoch:
                    node.value = None
                    node.next = None
                    self._free.append(node)
                    self.reclaimed += 1
                else:
                    kept.append((retire_epoch, node))
            bag[:] = kept

    def stats(self):
        with self._lock:
            limbo_size = sum(len(bag) for bag in self._limbo.values())
            return {
                "epoch": self._epoch,
                "retired": self.retired,
                "reclaimed": self.reclaimed,
                "limbo": limbo_size,
                "freelist": len(self._free),
            }


class LockFreeStack:
    """ABA 防护的无锁栈（Treiber stack）。"""

    def __init__(self, tag_modulus=None, reclaimer=None):
        self._reclaimer = reclaimer or EpochReclaimer()
        self._tag_modulus = tag_modulus  # None=不回绕（任意精度）；否则模拟定宽
        self._head = AtomicRef(Head(None, 0))

    def _bump(self, tag):
        tag += 1
        if self._tag_modulus is not None:
            tag %= self._tag_modulus
        return tag

    def push(self, value):
        node = self._reclaimer.alloc(value)
        while True:
            head = self._head.load()
            node.next = head.node
            # push 不解引用 head.node，只需读其身份，故无需 pin
            if self._head.compare_and_swap(
                head, Head(node, self._bump(head.tag))
            ):
                return

    def pop(self):
        while True:
            self._reclaimer.pin()  # 公告：下面要解引用 head.node.next
            head = self._head.load()
            node = head.node
            if node is None:
                self._reclaimer.unpin()
                return EMPTY
            nxt = node.next  # 受 pin 保护：节点不可能在此期间被回收复用
            if self._head.compare_and_swap(
                head, Head(nxt, self._bump(head.tag))
            ):
                value = node.value  # 必须在 retire 之前取值（retire 可能立即回收）
                self._reclaimer.unpin()
                self._reclaimer.retire(node)
                return value
            self._reclaimer.unpin()

    @property
    def reclaimer(self):
        return self._reclaimer


class LockedStack:
    """加锁参考实现，用于对拍。"""

    def __init__(self):
        self._items = []
        self._lock = threading.Lock()

    def push(self, value):
        with self._lock:
            self._items.append(value)

    def pop(self):
        with self._lock:
            if not self._items:
                return EMPTY
            return self._items.pop()


class UnsafeNoTagStack:
    """故意不做任何 ABA 防护的 Treiber 栈：裸指针 CAS + 立即复用。

    仅用于演示 ABA 危害，严禁用于任何真实场景。
    """

    def __init__(self):
        self._head = AtomicRef(None)
        self._free = []

    def push(self, value):
        if self._free:
            node = self._free.pop()
            node.value = value
        else:
            node = Node(value)
        while True:
            head = self._head.load()
            node.next = head
            if self._head.compare_and_swap(head, node):
                return

    def pop(self):
        while True:
            head = self._head.load()
            if head is None:
                return EMPTY
            nxt = head.next
            if self._head.compare_and_swap(head, nxt):
                value = head.value
                head.value = None
                head.next = None
                self._free.append(head)  # 立即回收复用 —— ABA 温床
                return value
