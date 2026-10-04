"""混合分发库:同一主题(topic)下同时支持广播与单播两种语义。

语义约定
--------
1. 两种语义分开处理:
   - publish_broadcast 把消息投递给发布时刻的**全部**订阅者;
   - publish_unicast   把消息投递给订阅者中的**恰好一个**。
2. 单播选择可复现(轮转 / round-robin):
   订阅者按订阅先后顺序排列,第 k 次单播(从 0 计)选中下标
   k % 当前订阅者数 的订阅者。选择只取决于操作序列,与机器、
   时间无关,因此完全可复现。
3. 订阅 / 退订期间不漏投:
   每次发布先对当前订阅者取**快照**,再按快照投递。发布过程中发生的
   订阅 / 退订(包括处理器内重入)只影响后续发布,不影响本次投递;
   本次消息既不漏投也不多投。
4. 订阅者为空时:广播是 no-op(返回 0),单播丢弃消息并返回 None。
"""

__all__ = ["Topic"]


class Topic:
    """一个支持广播 + 单播混合分发的主题。"""

    __slots__ = ("_subs", "_next_token", "_rr", "_cache")

    def __init__(self):
        self._subs = {}        # token -> handler, 按订阅顺序(插入序)
        self._next_token = 0   # 单调递增的订阅令牌
        self._rr = 0           # 已发生的单播次数(轮转游标)
        self._cache = None     # (tokens, handlers) 快照缓存, 订/退订时失效

    # ------------------------------------------------------------------
    # 订阅管理
    # ------------------------------------------------------------------
    def subscribe(self, handler):
        """订阅,返回令牌(token),用于退订。handler 签名为 handler(message)。"""
        if not callable(handler):
            raise TypeError("handler must be callable")
        token = self._next_token
        self._next_token += 1
        self._subs[token] = handler
        self._cache = None
        return token

    def unsubscribe(self, token):
        """退订。成功返回 True,令牌不存在返回 False(幂等,不抛异常)。"""
        if self._subs.pop(token, None) is None:
            return False
        self._cache = None
        return True

    @property
    def subscriber_count(self):
        return len(self._subs)

    def subscribers(self):
        """当前订阅者令牌,按订阅顺序。"""
        return tuple(self._subs.keys())

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def publish_broadcast(self, message):
        """广播:投递给发布时刻的全部订阅者,返回投递份数。"""
        _, handlers = self._snapshot()
        for handler in handlers:
            handler(message)
        return len(handlers)

    def publish_unicast(self, message):
        """单播:轮转选出一个订阅者投递,返回其令牌;无订阅者返回 None。"""
        tokens, handlers = self._snapshot()
        n = len(handlers)
        if n == 0:
            return None
        idx = self._rr % n
        self._rr += 1
        handlers[idx](message)
        return tokens[idx]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _snapshot(self):
        """当前订阅者的不可变快照(带缓存,订阅关系不变时 O(1))。"""
        if self._cache is None:
            items = tuple(self._subs.items())
            self._cache = (
                tuple(token for token, _ in items),
                tuple(handler for _, handler in items),
            )
        return self._cache
