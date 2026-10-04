"""进程内发布订阅，带订阅者异常隔离。

语义约定：
- 投递顺序确定：严格按订阅顺序（先订阅先投递）。
- 同步串行投递：同一个分发循环内逐个调用，不并发投递，
  因此顺序保证成立，且订阅者抛异常不会中断后续投递。
- 取消订阅立即生效：publish 进行中调用 unsubscribe 后，
  本轮剩余投递中该订阅者不会再被调用（按 token 逐个检查存活）。
- 本轮新订阅的订阅者不接收本轮事件（本轮订阅者集合在发布开始时确定）。
- 订阅者异常被隔离并全部汇总，随 PublishResult 返回给发布方。
"""

from __future__ import annotations

import itertools
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

Handler = Callable[[Any], None]


@dataclass(frozen=True)
class SubscriberError:
    """单个订阅者的异常记录：订阅者标识 + 原始错误。"""

    topic: str
    subscription_id: int
    handler_name: str
    error: BaseException          # 原始异常对象
    error_type: str               # 异常类名，便于日志/序列化
    traceback_str: str            # 完整堆栈字符串


@dataclass
class PublishResult:
    """一次发布的投递结果与异常汇总。"""

    topic: str
    event: Any
    delivered: List[int] = field(default_factory=list)          # 成功投递的订阅者 id（按投递顺序）
    errors: List[SubscriberError] = field(default_factory=list) # 全部异常，逐条记录
    skipped: List[int] = field(default_factory=list)            # 本轮内被取消而跳过的订阅者 id

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_failed(self) -> None:
        """可选：把汇总异常作为 ExceptionGroup 抛给发布方。"""
        if self.errors:
            raise ExceptionGroup(
                f"publish to {self.topic!r}: {len(self.errors)} subscriber(s) failed",
                [e.error for e in self.errors],
            )


class EventBus:
    def __init__(self) -> None:
        # topic -> OrderedDict[subscription_id, handler]，保证订阅顺序即投递顺序
        self._topics: Dict[str, "OrderedDict[int, Handler]"] = {}
        self._names: Dict[int, str] = {}
        self._id_gen = itertools.count(1)

    def subscribe(self, topic: str, handler: Handler, *, name: Optional[str] = None) -> int:
        """订阅主题，返回订阅 id（取消订阅时使用）。"""
        if not callable(handler):
            raise TypeError("handler must be callable")
        subs = self._topics.setdefault(topic, OrderedDict())
        sub_id = next(self._id_gen)
        subs[sub_id] = handler
        self._names[sub_id] = name or getattr(handler, "__qualname__", repr(handler))
        return sub_id

    def unsubscribe(self, topic: str, subscription_id: int) -> bool:
        """取消订阅，立即生效：本轮投递中也不会再调用该订阅者。"""
        subs = self._topics.get(topic)
        if subs and subscription_id in subs:
            del subs[subscription_id]
            self._names.pop(subscription_id, None)
            return True
        return False

    def publish(self, topic: str, event: Any = None) -> PublishResult:
        """同步按订阅顺序投递；单个订阅者异常被隔离，不影响其余订阅者。"""
        result = PublishResult(topic=topic, event=event)
        subs = self._topics.get(topic)
        if not subs:
            return result
        # 本轮候选集合在发布开始时确定（新订阅者不参与本轮），
        # 但每个订阅者调用前都检查是否仍存活，保证取消立即生效。
        for sub_id in list(subs.keys()):
            handler = subs.get(sub_id)
            if handler is None:
                result.skipped.append(sub_id)
                continue
            try:
                handler(event)
            except BaseException as exc:  # 隔离：不让异常逃出分发循环
                result.errors.append(
                    SubscriberError(
                        topic=topic,
                        subscription_id=sub_id,
                        handler_name=self._names.get(sub_id, "<unknown>"),
                        error=exc,
                        error_type=type(exc).__name__,
                        traceback_str=traceback.format_exc(),
                    )
                )
            else:
                result.delivered.append(sub_id)
        return result

    def subscriber_count(self, topic: str) -> int:
        return len(self._topics.get(topic, ()))
