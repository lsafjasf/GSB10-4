"""TCP 风格保活 / 探测判定库（纯标准库，时间由外部注入）。

设计要点
========
判定只依赖「注入的时间 now」与「事件流」，不读真实时钟，因此完全可复现。

重置规则（哪些事件重置空闲计时）
--------------------------------
RESET_EVENTS（入站证据，证明对端仍存活，重置 last_activity 并清空探测状态）：
  - RX_DATA       收到对端的应用数据
  - RX_PROBE_ACK  收到对端对保活探测的应答

NON_RESET_EVENTS（不重置空闲计时）：
  - TX_DATA       本端发出应用数据：写成功只说明本地未报错，数据可能
                   只进入发送缓冲，对端未必可达（这正是单向半开情形），
                   因此 TX 一律不重置
  - TX_PROBE      本端发出探测包（自己说话不能证明对端活着）
  - TICK          定时器推进
  任何其他事件一律不重置

状态机
------
ESTABLISHED --(now - last_activity >= idle_timeout)--> PROBING（发出第 1 个探测）
PROBING     --(now - last_probe >= probe_interval)-->
    若 probes_sent >= max_probes: DEAD（判定断开）
    否则: 发出下一个探测, probes_sent += 1
ESTABLISHED/PROBING --(RESET 事件)--> ESTABLISHED（probes_sent 清零）
DEAD        终态，任何事件/tick 均不再改变（判死后必须重建连接）

断开阈值（确定性规则）
--------------------
从最后一次保活事件起，判定断开所需的最短时间为：
    T_dead = idle_timeout + max_probes * probe_interval
即：空闲满 idle_timeout 后开始探测，共发 max_probes 个探测，
第 max_probes 个探测发出后再等满一个 probe_interval 仍无应答 -> DEAD。

边界（tie-break）规则：所有比较一律使用 >=（左闭右开）。
  - now - last_activity == idle_timeout 恰好相等 -> 立即进入 PROBING。
  - now - last_probe   == probe_interval 恰好相等 -> 立即推进探测/判死。
  - RESET 事件与 tick 落在同一时刻时，先处理事件再 tick（事件优先）。
  - tick 为追赶式：探测时刻锚定在 last_activity + idle_timeout + k*interval，
    判定结果与 tick 调用粒度无关（逐秒轮询与一次性快进到同一时刻等价）。
"""

from __future__ import annotations

import enum


class Event(enum.Enum):
    RX_DATA = "RX_DATA"                # 重置
    TX_DATA = "TX_DATA"                # 重置
    RX_PROBE_ACK = "RX_PROBE_ACK"      # 重置
    TX_PROBE = "TX_PROBE"              # 不重置（由引擎自身产生，仅作记录）
    TICK = "TICK"                      # 不重置


RESET_EVENTS = frozenset({Event.RX_DATA, Event.RX_PROBE_ACK})
NON_RESET_EVENTS = frozenset({Event.TX_DATA, Event.TX_PROBE, Event.TICK})


class State(enum.Enum):
    ESTABLISHED = "ESTABLISHED"
    PROBING = "PROBING"
    DEAD = "DEAD"


class Connection:
    """单条连接的保活状态。__slots__ 控制内存上界。"""

    __slots__ = ("state", "last_activity", "last_probe", "probes_sent")

    def __init__(self, now: float) -> None:
        self.state = State.ESTABLISHED
        self.last_activity = now   # 最近一次 RESET 事件的时间
        self.last_probe = now      # 最近一次发出探测的时间
        self.probes_sent = 0       # 本轮 PROBING 已发出的探测数


class KeepaliveEngine:
    """保活判定引擎。所有时间均由调用方注入。"""

    def __init__(
        self,
        idle_timeout: float = 30.0,
        probe_interval: float = 5.0,
        max_probes: int = 3,
    ) -> None:
        if idle_timeout <= 0:
            raise ValueError("idle_timeout must be > 0")
        if probe_interval <= 0:
            raise ValueError("probe_interval must be > 0")
        if max_probes < 1:
            raise ValueError("max_probes must be >= 1")
        self.idle_timeout = float(idle_timeout)
        self.probe_interval = float(probe_interval)
        self.max_probes = int(max_probes)

    @property
    def dead_after(self) -> float:
        """从最后保活事件到判死的最短时长。"""
        return self.idle_timeout + self.max_probes * self.probe_interval

    def on_event(self, conn: Connection, event: Event, now: float) -> None:
        """处理一个外部事件。事件优先于同一时刻的 tick。DEAD 为终态。"""
        if conn.state is State.DEAD:
            return
        if event in RESET_EVENTS:
            conn.state = State.ESTABLISHED
            conn.last_activity = now
            conn.last_probe = now
            conn.probes_sent = 0
        # 非重置事件不改变任何状态

    def tick(self, conn: Connection, now: float) -> None:
        """推进定时器到 now（追赶式：一次 tick 可补发多个探测）。

        探测时刻锚定在 last_activity + idle_timeout + k*probe_interval，
        因此判定结果与 tick 的调用粒度无关，只取决于注入的时间与事件。
        """
        if conn.state is State.DEAD:
            return
        if conn.state is State.ESTABLISHED:
            if now - conn.last_activity < self.idle_timeout:
                return
            conn.state = State.PROBING
            conn.probes_sent = 1
            conn.last_probe = conn.last_activity + self.idle_timeout
        # PROBING：追赶所有到期的探测
        while now - conn.last_probe >= self.probe_interval:
            if conn.probes_sent >= self.max_probes:
                conn.state = State.DEAD
                return
            conn.probes_sent += 1
            conn.last_probe += self.probe_interval

    def tick_many(self, conns, now: float) -> None:
        """批量推进（10 万级连接每秒判定入口），语义与 tick 完全一致。"""
        idle_timeout = self.idle_timeout
        probe_interval = self.probe_interval
        max_probes = self.max_probes
        dead = State.DEAD
        probing = State.PROBING
        for conn in conns:
            state = conn.state
            if state is dead:
                continue
            if state is probing:
                last_probe = conn.last_probe
                probes_sent = conn.probes_sent
                while now - last_probe >= probe_interval:
                    if probes_sent >= max_probes:
                        conn.state = dead
                        break
                    probes_sent += 1
                    last_probe += probe_interval
                else:
                    conn.probes_sent = probes_sent
                    conn.last_probe = last_probe
            elif now - conn.last_activity >= idle_timeout:
                conn.state = probing
                conn.probes_sent = 1
                conn.last_probe = conn.last_activity + idle_timeout


def simulate(engine: KeepaliveEngine, script, now0: float = 0.0):
    """按 [(t, Event|None), ...] 脚本回放，返回每个时刻之后的状态序列。

    每个条目先处理事件（可为 None 表示纯 tick），再执行 tick，
    返回 [(t, State, probes_sent), ...]，作为可复现的判定序列。
    """
    conn = Connection(now0)
    trace = []
    for t, event in script:
        if event is not None:
            engine.on_event(conn, event, t)
        engine.tick(conn, t)
        trace.append((t, conn.state, conn.probes_sent))
    return trace
