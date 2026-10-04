"""混合分发库：同一主题下广播与单播两种语义分开处理。

语义约定：
- 广播：对分发时刻的订阅者快照逐人投递，每人一份。
- 单播：按订阅顺序轮转（round-robin）选出恰好一名订阅者投递，可复现。
- 订阅/退订立即生效：边界点之前发起的分发看不到新订阅者，
  边界点之后发起的分发必须包含新订阅者、不得再投给已退订者。
  分发进行中（回调执行期间）的订阅变更只影响后续分发，不影响本次快照。
- 单播轮转游标规则（保证连续序列可复现）：
    * 退订者位于游标之前时游标前移一格，保证“下一个该谁”不因退订而跳变；
    * 退订者位于游标处时游标原地指向后一位（取模），不跳过任何人；
    * 新订阅者追加到队尾，本轮轮转结束后才轮到它。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class Mode(Enum):
    BROADCAST = "broadcast"
    UNICAST = "unicast"


@dataclass(frozen=True)
class Message:
    topic: str
    mode: Mode
    seq: int
    payload: Any = None


@dataclass
class DispatchResult:
    message: Message
    delivered: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.delivered)


class Topic:
    """一个主题：广播投全部订阅者，单播轮转选一名。"""

    def __init__(self, name: str):
        self.name = name
        self._subs: Dict[str, Callable[[Message], None]] = {}
        self._order: List[str] = []  # 订阅顺序
        self._cursor = 0             # 单播轮转游标（相对 _order）
        self._seq = 0

    # ---- 订阅管理 ----

    def subscribe(self, sub_id: str, callback: Callable[[Message], None]) -> None:
        if sub_id in self._subs:
            raise ValueError(f"subscriber already exists: {sub_id}")
        self._subs[sub_id] = callback
        self._order.append(sub_id)

    def unsubscribe(self, sub_id: str) -> bool:
        if sub_id not in self._subs:
            return False
        idx = self._order.index(sub_id)
        del self._subs[sub_id]
        del self._order[idx]
        if idx < self._cursor:
            self._cursor -= 1
        elif idx == self._cursor:
            self._cursor %= len(self._order) if self._order else 1
        return True

    def subscribers(self) -> List[str]:
        return list(self._order)

    # ---- 分发 ----

    def broadcast(self, payload: Any = None) -> DispatchResult:
        return self._dispatch(Mode.BROADCAST, payload)

    def unicast(self, payload: Any = None) -> DispatchResult:
        return self._dispatch(Mode.UNICAST, payload)

    def _dispatch(self, mode: Mode, payload: Any) -> DispatchResult:
        self._seq += 1
        msg = Message(topic=self.name, mode=mode, seq=self._seq, payload=payload)
        result = DispatchResult(message=msg)
        # 连回调一起快照：快照点的订阅者必须投到，分发期间变更不影响本次
        if mode is Mode.BROADCAST:
            targets = [(sid, self._subs[sid]) for sid in self._order]
        else:
            if not self._order:
                targets = []             # 无订阅者：单播丢弃，不报错
            else:
                target = self._order[self._cursor]
                self._cursor = (self._cursor + 1) % len(self._order)
                targets = [(target, self._subs[target])]
        for sub_id, callback in targets:
            callback(msg)
            result.delivered.append(sub_id)
        return result
