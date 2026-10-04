"""单播轮转选择序列(可复现),并覆盖订阅边界上的投递结果。

运行: python3 selection_sequence.py  ->  selection_sequence.txt

轮转规则(与 mixed_dispatch.Topic 一致):
    第 k 次单播选中订阅顺序中第 (k % 当前订阅者数) 个订阅者。
"""

from mixed_dispatch import Topic


class Mailbox:
    def __init__(self, name):
        self.name = name
        self.messages = []

    def __call__(self, msg):
        self.messages.append(msg)


def scenario():
    log = []
    t = Topic()
    boxes = {}

    def add(name):
        box = Mailbox(name)
        token = t.subscribe(box)
        boxes[token] = box
        log.append("subscribe  token=%d name=%s" % (token, name))
        return token

    def remove(token):
        t.unsubscribe(token)
        log.append("unsubscribe token=%d name=%s" % (token, boxes[token].name))

    def unicast(label):
        token = t.publish_unicast(label)
        chosen = None if token is None else boxes[token].name
        log.append("unicast %-4s -> chosen=%s (subscribers=%d)"
                   % (label, chosen, t.subscriber_count))

    def broadcast(label):
        n = t.publish_broadcast(label)
        got = [b.name for b in boxes.values() if label in b.messages]
        log.append("broadcast %-4s -> delivered=%d receivers=%s"
                   % (label, n, got))

    log.append("== phase 1: 空主题上发布(订阅者为空) ==")
    unicast("u00")
    broadcast("b00")

    log.append("== phase 2: 依次订阅 S0,S1,S2,连续 10 次单播 ==")
    add("S0"); add("S1"); add("S2")
    for i in range(10):
        unicast("u%02d" % (i + 1))

    log.append("== phase 3: 订阅边界 -- S2 退订后立刻单播 ==")
    t2 = None
    for token, box in boxes.items():
        if box.name == "S2":
            t2 = token
    remove(t2)
    unicast("u11")
    unicast("u12")

    log.append("== phase 4: 订阅边界 -- 新订阅 S3 后立刻单播 ==")
    add("S3")
    unicast("u13")
    unicast("u14")

    log.append("== phase 5: 全部退订,空主题发布,然后重新订阅 S4 ==")
    remove(t2) if t2 in t.subscribers() else None
    for token in t.subscribers():
        remove(token)
    unicast("u15")
    broadcast("b01")
    add("S4")
    unicast("u16")
    unicast("u17")

    log.append("== phase 6: 广播到全部订阅者 ==")
    add("S5")
    broadcast("b02")

    # 每个订阅者的最终收信明细(单播部分)
    log.append("== 每个订阅者实际收到的全部消息 ==")
    for token, box in sorted(boxes.items()):
        log.append("token=%d name=%s messages=%s"
                   % (token, box.name, box.messages))
    return log


def main():
    lines = scenario()
    text = "\n".join(lines)
    with open("selection_sequence.txt", "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
