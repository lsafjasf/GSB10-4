"""订阅确认与未确认窗口（仅标准库）。

模型：推送方按单调递增序号推送消息，订阅方回 ACK，ACK 可能乱序、重复、
迟到或永远不到。窗口维护一个连续前缀水位（已确认的最大连续序号）和一个
乱序集合（水位之上、已被确认但尚未连上的序号），未确认数 = 水位之上
尚未被确认的序号个数。

超限策略（max_unacked 到达上限时 push 的行为）：
- PAUSE      阻塞等待，直到未确认数低于上限（推送方暂停）。
- DROP_NEW   丢弃新消息，push 返回 False，旧消息保留等待确认；
             被丢弃的序号记为"空洞"，水位推进时跳过（它永远不会被确认）。
- DISCONNECT 抛出 WindowFullError，由调用方断开订阅连接。
"""

from enum import Enum
from threading import Condition, RLock

__all__ = [
    "OverflowPolicy",
    "WindowFullError",
    "DuplicateAckError",
    "AckWindow",
]


class OverflowPolicy(Enum):
    PAUSE = "pause"
    DROP_NEW = "drop_new"
    DISCONNECT = "disconnect"


class WindowFullError(Exception):
    """超限策略为 DISCONNECT 时，窗口满仍 push 抛出本异常，调用方应断开连接。"""


class DuplicateAckError(Exception):
    """strict=True 时收到重复 ACK 抛出（默认幂等忽略，不抛）。"""


class AckWindow:
    """未确认窗口：乱序确认可推进水位，重复确认幂等。"""

    def __init__(self, max_unacked, policy=OverflowPolicy.PAUSE, strict=False):
        if max_unacked < 1:
            raise ValueError("max_unacked 必须 >= 1")
        self.max_unacked = max_unacked
        self.policy = policy
        self.strict = strict
        self._lock = RLock()
        self._not_full = Condition(self._lock)
        # 水位：序号 <= _base 的消息全部已确认
        self._base = 0
        # 乱序集合：序号 > _base 且已确认、尚未连上水位的序号
        self._acked_ahead = set()
        # 未确认数：序号 > _base 且未确认的个数
        self._unacked = 0
        # 已进入窗口的最大序号（DROP_NEW 会造成序号空洞，不能用水位+计数推断）
        self._max_pushed = 0
        # DROP_NEW 丢弃的序号集合：永远不会被确认，水位推进时跳过
        self._gaps = set()
        # 计数器
        self.pushed = 0            # 成功进入窗口的推送数
        self.dropped = 0           # DROP_NEW 策略下被丢弃的新消息数
        self.acks_accepted = 0     # 首次生效的 ACK 数（去重后）
        self.acks_duplicate = 0    # 重复 ACK 数（幂等忽略，不重复扣减）

    # ---- 推送方 ----

    def push(self, seq):
        """推送一条消息。返回 True 表示进入窗口；DROP_NEW 超限时返回 False。

        PAUSE 超限时阻塞至窗口腾出位置；DISCONNECT 超限时抛 WindowFullError。
        """
        with self._not_full:
            while self._unacked >= self.max_unacked:
                if self.policy is OverflowPolicy.PAUSE:
                    self._not_full.wait()
                elif self.policy is OverflowPolicy.DROP_NEW:
                    self.dropped += 1
                    self._gaps.add(seq)
                    return False
                else:
                    raise WindowFullError(
                        "未确认数 %d 达到上限 %d，按 DISCONNECT 策略应断开订阅方"
                        % (self._unacked, self.max_unacked)
                    )
            if seq <= self._base:
                raise ValueError("序号 %d 不高于水位 %d：推送序号必须单调递增" % (seq, self._base))
            self._unacked += 1
            self._max_pushed = seq
            self.pushed += 1
            return True

    # ---- 订阅方 ----

    def ack(self, seq):
        """处理一个 ACK。返回 True 表示首次生效，False 表示重复（幂等忽略）。

        重复 ACK 不重复扣减未确认数；strict=True 时改为抛 DuplicateAckError。
        """
        with self._not_full:
            if seq <= self._base or seq in self._acked_ahead or seq in self._gaps:
                # 已确认过或已被丢弃：幂等，计数不重复扣减
                self.acks_duplicate += 1
                if self.strict:
                    raise DuplicateAckError("重复 ACK: seq=%d" % seq)
                return False
            if seq > self._max_pushed:
                raise ValueError("ACK 序号 %d 超出已推送范围" % seq)
            # 首次生效的 ACK
            self._unacked -= 1
            self.acks_accepted += 1
            if seq == self._base + 1:
                # 连上水位：推进连续前缀，吸收乱序集合中已连通的序号，
                # 并跳过 DROP_NEW 留下的空洞（空洞序号永远不会被确认）
                base = seq + 1
                ahead = self._acked_ahead
                gaps = self._gaps
                while True:
                    if base in ahead:
                        ahead.discard(base)
                    elif base in gaps:
                        gaps.discard(base)
                    else:
                        break
                    base += 1
                self._base = base - 1
            else:
                # 乱序 ACK：先挂到水位之上，等空洞补齐后随水位一起推进
                self._acked_ahead.add(seq)
            self._not_full.notify_all()
            return True

    # ---- 观测 ----

    @property
    def base(self):
        """连续确认水位：序号 <= base 的消息全部已确认。"""
        with self._lock:
            return self._base

    @property
    def unacked(self):
        """当前未确认数。"""
        with self._lock:
            return self._unacked

    @property
    def buffered_acks(self):
        """乱序集合大小（已确认但水位未推进到的序号个数）。"""
        with self._lock:
            return len(self._acked_ahead)

    def trace(self):
        """当前计数轨迹快照。"""
        with self._lock:
            return {
                "base": self._base,
                "unacked": self._unacked,
                "buffered_acks": len(self._acked_ahead),
                "gaps": len(self._gaps),
                "pushed": self.pushed,
                "dropped": self.dropped,
                "acks_accepted": self.acks_accepted,
                "acks_duplicate": self.acks_duplicate,
            }
