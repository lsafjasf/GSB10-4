"""TCP 风格保活 / 探测判定库（仅标准库，时间由外部注入）。

判定模型（确定性，全部只依赖注入的时间与事件）：

    参数：
        idle_timeout    空闲阈值 T（秒）：距上次“有效活动”超过 T 即进入探测。
        probe_interval  探测间隔 I（秒）：进入探测后每隔 I 秒发一次探测。
        probe_count     探测次数 C：发满 C 次探测仍无有效活动则判定断开。

    时间线（last = 最后一次有效活动时刻）：
        t <  last + T                : ALIVE，不发探测
        t == last + T + k*I (k=0..C-1): 发出第 k+1 次探测
        t >= last + T + C*I          : 判定 DEAD（断开阈值 = T + C*I）

    重置空闲计时的事件（有效活动）：
        send_data / recv_data（应用层数据，n > 0）
    不重置的事件：
        send_probe（自身发出的探测）、recv_ack（纯 ACK / 保活应答，0 字节数据）、
        advance（时钟推进）、state（查询）

实现：最小堆（惰性删除）按“下一次到期时刻”排序，advance 只处理到期连接，
单连接均摊 O(log N)，空闲连接零开销。
"""

import heapq

ALIVE = "ALIVE"
DEAD = "DEAD"


class Connection:
    __slots__ = ("last_activity", "probes_sent", "state", "version")

    def __init__(self, t):
        self.last_activity = t
        self.probes_sent = 0
        self.state = ALIVE
        self.version = 0  # 堆条目惰性删除用的版本号


class KeepAliveMonitor:
    def __init__(self, idle_timeout, probe_interval, probe_count):
        if idle_timeout <= 0 or probe_interval <= 0 or probe_count <= 0:
            raise ValueError("idle_timeout / probe_interval / probe_count 必须为正数")
        self.idle_timeout = float(idle_timeout)
        self.probe_interval = float(probe_interval)
        self.probe_count = int(probe_count)
        self._conns = {}
        self._heap = []  # (next_due, version, conn_id)

    # ---- 内部 ----

    def _due(self, conn):
        # 下一次到期时刻：未发满探测 -> 下一次探测时刻；发满 -> 断开阈值时刻
        return conn.last_activity + self.idle_timeout + conn.probes_sent * self.probe_interval

    def _push(self, conn_id, conn):
        conn.version += 1
        heapq.heappush(self._heap, (self._due(conn), conn.version, conn_id))

    def _touch(self, conn_id, t):
        """有效活动：重置空闲计时与探测计数。"""
        conn = self._conns[conn_id]
        conn.last_activity = t
        conn.probes_sent = 0
        self._push(conn_id, conn)

    # ---- 连接生命周期 ----

    def add(self, conn_id, t):
        if conn_id in self._conns:
            raise KeyError("连接已存在: %r" % (conn_id,))
        conn = Connection(t)
        self._conns[conn_id] = conn
        self._push(conn_id, conn)

    def remove(self, conn_id):
        del self._conns[conn_id]  # 堆条目靠版本号惰性失效

    # ---- 事件（重置计时） ----

    def send_data(self, conn_id, n, t):
        if n <= 0:
            raise ValueError("数据事件字节数必须 > 0")
        self._touch(conn_id, t)

    def recv_data(self, conn_id, n, t):
        if n <= 0:
            raise ValueError("数据事件字节数必须 > 0")
        self._touch(conn_id, t)

    # ---- 事件（不重置计时） ----

    def recv_ack(self, conn_id, t):
        """纯 ACK / 保活应答：不携带应用数据，不重置空闲计时。"""
        pass

    # ---- 时钟推进 ----

    def advance(self, t):
        """把时钟推进到 t，返回按 (时刻, conn_id) 排序的确定性事件序列。

        事件形式：("probe", conn_id, k, t_k) 第 k 次探测（1 起）
                  ("dead",  conn_id, t_dead)  判定断开
        """
        events = []
        heap = self._heap
        while heap and heap[0][0] <= t:
            due, version, conn_id = heapq.heappop(heap)
            conn = self._conns.get(conn_id)
            if conn is None or conn.version != version or conn.state == DEAD:
                continue
            if conn.probes_sent < self.probe_count:
                conn.probes_sent += 1
                events.append(("probe", conn_id, conn.probes_sent, due))
                self._push(conn_id, conn)
            else:
                conn.state = DEAD
                events.append(("dead", conn_id, due))
        events.sort(key=lambda e: (e[-1], e[1]))
        return events

    # ---- 查询 ----

    def state(self, conn_id):
        return self._conns[conn_id].state

    def disconnect_threshold(self, conn_id):
        """该连接若从此刻起再无有效活动，被判 DEAD 的绝对时刻。"""
        conn = self._conns[conn_id]
        return conn.last_activity + self.idle_timeout + self.probe_count * self.probe_interval

    def alive_count(self):
        return sum(1 for c in self._conns.values() if c.state == ALIVE)
