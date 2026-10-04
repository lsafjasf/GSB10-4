"""AgingPriorityQueue: 带老化（aging）防饿死机制的异步任务优先级队列。

仅使用 Python 标准库。时间通过 `clock` 注入，便于确定性测试。

公平性核心思想（静态虚拟键）
------------------------------
任务在入队时计算一个**静态**排序键：

    K = priority * aging_interval + enqueue_time

堆顶永远是 K 最小的任务，K 相同按入队序号（严格 FIFO）。
这等价于经典老化规则「有效优先级 = priority - floor(wait / aging_interval)」，
因为对所有任务减去同一个 now/aging_interval 不改变相对顺序——
所以无需周期性重排堆，键在入队后永不变化，push/pop 均为 O(log n)。

有界性（防饿死证明要点）
------------------------
设任务 T 在时刻 t 以优先级 p 入队，K_T = p*A + t。
任何在时刻 t' > t + p*A 之后入队的任务（包括优先级 0 的洪水任务），
其键 K = p'*A + t' >= t' > K_T，永远排在 T 之后。
因此能插在 T 前面的任务全部入队于有限窗口 [0, t + p*A] 内，数量有界。
记该窗口结束时队列中排在 T 前的任务数为 B，单次服务耗时 S，则：

    completion(T) <= t + p*A + (B + 1) * S

即使高优先级洪水无限持续，T 也必在有界时间内被调度——不会饿死。
"""

import heapq
import time

__all__ = ["AgingPriorityQueue", "Task", "Empty"]


class Empty(Exception):
    """Raised by dequeue() when no live task remains in the queue."""


class Task:
    """出队返回的任务对象（不可变）。"""

    __slots__ = ("task_id", "payload", "priority", "enqueue_time")

    def __init__(self, task_id, payload, priority, enqueue_time):
        self.task_id = task_id
        self.payload = payload
        self.priority = priority
        self.enqueue_time = enqueue_time

    def __repr__(self):
        return (
            "Task(task_id=%r, priority=%r, enqueue_time=%r)"
            % (self.task_id, self.priority, self.enqueue_time)
        )


class _Entry:
    __slots__ = ("key", "seq", "task_id", "payload", "priority", "enqueue_time",
                 "deadline", "cancelled")

    def __init__(self, key, seq, task_id, payload, priority, enqueue_time, deadline):
        self.key = key
        self.seq = seq
        self.task_id = task_id
        self.payload = payload
        self.priority = priority
        self.enqueue_time = enqueue_time
        self.deadline = deadline
        self.cancelled = False


class AgingPriorityQueue:
    """带老化机制的公平优先级队列。

    参数:
        num_priorities: 优先级档位数，0 为最高优先级。
        aging_interval: 老化间隔 A。每等待 A 个时间单位，有效优先级提升一级。
        clock: 可注入的时钟（无参可调用，返回单调递增的浮点时间）。
    """

    def __init__(self, num_priorities=8, aging_interval=60.0, clock=None):
        if num_priorities < 1:
            raise ValueError("num_priorities must be >= 1")
        if aging_interval <= 0:
            raise ValueError("aging_interval must be > 0")
        self._num_priorities = num_priorities
        self._aging = float(aging_interval)
        self._clock = clock if clock is not None else time.monotonic
        self._heap = []          # [(key, seq, _Entry)]，seq 唯一，entry 不参与比较
        self._entries = {}       # task_id -> _Entry（仅存活任务）
        self._seq = 0

    @property
    def aging_interval(self):
        return self._aging

    def __len__(self):
        """队列中存活（未取消、未超时、未出队）的任务数。"""
        return len(self._entries)

    def __contains__(self, task_id):
        return task_id in self._entries

    def enqueue(self, task_id, payload=None, priority=0, timeout=None):
        """入队一个任务。

        参数:
            task_id: 唯一任务标识（存活期间不得重复）。
            payload: 任意附加数据。
            priority: 基础优先级，0 最高。
            timeout: 相对超时时间；到期后任务视为失效，出队时跳过。
        """
        if task_id in self._entries:
            raise ValueError("duplicate task_id: %r" % (task_id,))
        if not 0 <= priority < self._num_priorities:
            raise ValueError("priority out of range: %r" % (priority,))
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be >= 0")
        now = self._clock()
        key = priority * self._aging + now
        deadline = now + timeout if timeout is not None else None
        entry = _Entry(key, self._seq, task_id, payload, priority, now, deadline)
        self._seq += 1
        self._entries[task_id] = entry
        heapq.heappush(self._heap, (key, entry.seq, entry))
        return task_id

    def dequeue(self):
        """取出当前应调度的任务；已取消/已超时的任务被惰性清除。

        返回 Task；队列为空时抛出 Empty。
        """
        now = self._clock()
        heap = self._heap
        entries = self._entries
        while heap:
            _, _, entry = heapq.heappop(heap)
            if entry.cancelled:
                continue
            if entry.deadline is not None and now >= entry.deadline:
                del entries[entry.task_id]
                continue
            del entries[entry.task_id]
            return Task(entry.task_id, entry.payload, entry.priority,
                        entry.enqueue_time)
        raise Empty("dequeue from empty queue")

    def cancel(self, task_id):
        """取消一个仍在队列中的任务。返回是否取消成功。

        采用惰性删除：仅打标记并移出索引，堆内条目在后续 dequeue 时清除，
        因此取消是 O(1)，且不触碰堆结构与其他任务的相对顺序。
        """
        entry = self._entries.pop(task_id, None)
        if entry is None:
            return False
        entry.cancelled = True
        return True
