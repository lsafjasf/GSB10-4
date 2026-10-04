"""对拍(differential test):库的实际分发结果 vs 逐条手工投递的期望结果。

参考模型 RefModel 与 mixed_dispatch.Topic 完全独立实现:
用一个朴素列表维护订阅顺序,每条消息逐条按语义手工投递
(广播:遍历全部邮箱;单播:rr % 人数 选一个邮箱)。

运行: python3 diff_test.py
产物: diff_data.json  (操作序列、每次单播的期望/实际选择、每个邮箱的
      期望/实际投递明细、逐消息是否一致)
"""

import json
import random

from mixed_dispatch import Topic

SEED = 20261005
N_OPS = 3000


class RefModel:
    """逐条手工投递的参考实现(刻意写得最直白)。"""

    def __init__(self):
        self.alive = []          # 存活订阅者 [(token, mailbox)], 订阅顺序
        self.mailboxes = {}      # token -> 历史邮箱(退订后保留历史)
        self.next_token = 0
        self.rr = 0              # 已发生的单播次数

    def subscribe(self):
        token = self.next_token
        self.next_token += 1
        box = []
        self.mailboxes[token] = box
        self.alive.append((token, box))
        return token

    def unsubscribe(self, token):
        before = len(self.alive)
        self.alive = [(t, b) for (t, b) in self.alive if t != token]
        return len(self.alive) < before

    def publish_broadcast(self, msg):
        # 逐条手工投递给当时的全部存活订阅者
        for _, box in self.alive:
            box.append(msg)
        return len(self.alive)

    def publish_unicast(self, msg):
        if not self.alive:
            return None
        idx = self.rr % len(self.alive)
        self.rr += 1
        self.alive[idx][1].append(msg)
        return self.alive[idx][0]


def run_scenario(seed=SEED, n_ops=N_OPS):
    """用同一操作序列驱动库与参考模型,收集对拍数据。"""
    rng = random.Random(seed)
    topic = Topic()
    ref = RefModel()

    lib_boxes = {}      # token -> 库侧邮箱
    ops = []            # 可复现的操作流水
    choices = []        # 每次单播: 期望选择 vs 实际选择
    per_msg = []        # 每次发布: 期望/实际接收者
    b_id = u_id = 0
    stats = {"sub": 0, "unsub": 0, "drain": 0, "broadcast": 0, "unicast": 0,
             "unicast_empty": 0, "broadcast_empty": 0}

    for step in range(n_ops):
        r = rng.random()
        n_alive = topic.subscriber_count

        if n_alive > 0 and step > 0 and step % 600 == 200:
            # 周期性全部退订(随机顺序),刻意制造空主题边界
            drained = list(topic.subscribers())
            rng.shuffle(drained)
            for token in drained:
                assert topic.unsubscribe(token)
                assert ref.unsubscribe(token)
            stats["drain"] += 1
            ops.append({"step": step, "op": "drain",
                        "tokens": drained})
            continue

        if r < 0.28:
            box = []
            token = topic.subscribe(box.append)
            lib_boxes[token] = box
            ref.subscribe()
            stats["sub"] += 1
            ops.append({"step": step, "op": "subscribe", "token": token})

        elif r < 0.48 and n_alive > 0:
            token = rng.choice(topic.subscribers())
            ok_lib = topic.unsubscribe(token)
            ok_ref = ref.unsubscribe(token)
            assert ok_lib and ok_ref
            stats["unsub"] += 1
            ops.append({"step": step, "op": "unsubscribe", "token": token})

        elif r < 0.75:
            msg = ["B", b_id]
            b_id += 1
            expected = [t for (t, _) in ref.alive]
            exp_count = ref.publish_broadcast(msg)
            act_count = topic.publish_broadcast(msg)
            stats["broadcast"] += 1
            if not expected:
                stats["broadcast_empty"] += 1
            per_msg.append({"step": step, "kind": "B", "msg": msg,
                            "expected": expected, "actual_count": act_count})
            ops.append({"step": step, "op": "broadcast", "msg": msg})

        else:
            msg = ["U", u_id]
            u_id += 1
            expected = ref.publish_unicast(msg)
            actual = topic.publish_unicast(msg)
            stats["unicast"] += 1
            if expected is None:
                stats["unicast_empty"] += 1
            choices.append({"step": step, "msg": msg,
                            "expected": expected, "actual": actual})
            per_msg.append({"step": step, "kind": "U", "msg": msg,
                            "expected": expected, "actual": actual})
            ops.append({"step": step, "op": "unicast", "msg": msg})

    # 逐邮箱对拍:每个订阅者收到的完整消息序列必须与手工投递一致
    box_compare = []
    all_match = True
    for token, exp_box in sorted(ref.mailboxes.items()):
        act_box = lib_boxes.get(token, [])
        match = (exp_box == act_box)
        all_match &= match
        box_compare.append({
            "token": token,
            "expected_count": len(exp_box),
            "actual_count": len(act_box),
            "match": match,
            # 邮箱可能很长,只在数据文件里放前 20 条供人工抽查
            "expected_head": exp_box[:20],
            "actual_head": act_box[:20],
        })

    choices_match = all(c["expected"] == c["actual"] for c in choices)
    counts_match = all(
        (c["kind"] == "B" and c["actual_count"] == len(c["expected"]))
        or (c["kind"] == "U" and c["actual"] == c["expected"])
        for c in per_msg
    )

    return {
        "seed": seed,
        "n_ops": n_ops,
        "rule": "unicast index = (#unicast_so_far) % (#subscribers_at_publish); "
                "broadcast = all subscribers at publish time (snapshot)",
        "stats": stats,
        "ops": ops,
        "per_message": per_msg,
        "unicast_choices": choices,
        "mailboxes": box_compare,
        "match": bool(all_match and choices_match and counts_match),
        "n_unicast_choices": len(choices),
        "n_mailboxes": len(box_compare),
    }


def main():
    data = run_scenario()
    with open("diff_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("seed=%d ops=%d" % (data["seed"], data["n_ops"]))
    print("stats:", data["stats"])
    print("unicast choices checked:", data["n_unicast_choices"])
    print("mailboxes checked:", data["n_mailboxes"])
    print("MATCH:", data["match"])
    raise SystemExit(0 if data["match"] else 1)


if __name__ == "__main__":
    main()
