"""独立参考实现：与 dispatcher.py 语义一致，但实现策略不同。

dispatcher.py 用“下标游标 + 退订时调整游标”；这里用“轮转队列”：
- upcoming 是本轮转剩余待投递的订阅者队列，单播总是取队首；
- 退订时把该订阅者从 upcoming 即时移除（不维护任何游标）；
- 新订阅者（含退订后重订阅）在本轮未结束时追加到本轮队尾，
  本轮已结束（upcoming 为空）时由下一轮重建按订阅顺序自然纳入；
- upcoming 为空时按当前订阅顺序重建一整轮。

维护不变式：upcoming == 库中 order[cursor:]，两种实现逐次分发结果必然一致。
"""


class ReferenceTopic:
    def __init__(self, name):
        self.name = name
        self.members = set()
        self.order = []      # 订阅顺序（仅存活成员）
        self.upcoming = []   # 本轮尚未投递的订阅者（仅存活成员）
        self.seq = 0

    def subscribe(self, sub_id):
        if sub_id in self.members:
            raise ValueError(f"subscriber already exists: {sub_id}")
        self.members.add(sub_id)
        self.order.append(sub_id)
        if self.upcoming:
            self.upcoming.append(sub_id)  # 本轮未结束：排到本轮队尾

    def unsubscribe(self, sub_id):
        if sub_id not in self.members:
            return False
        self.members.discard(sub_id)
        self.order.remove(sub_id)
        if sub_id in self.upcoming:
            self.upcoming.remove(sub_id)
        return True

    def dispatch(self, mode):
        self.seq += 1
        if mode == "broadcast":
            return list(self.order)
        # unicast
        if not self.upcoming:
            if not self.members:
                return []
            self.upcoming = list(self.order)  # 本轮结束：重建一整轮
        return [self.upcoming.pop(0)]
