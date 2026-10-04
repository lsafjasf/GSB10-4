"""生成单播选择序列数据（data/selection_sequence.txt）与订阅边界投递数据
（data/boundary_log.txt）。数据由本脚本确定性生成，可重复复现。"""

import os

from dispatcher import Topic

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def selection_sequence():
    """含成员变更的单播轮转序列：验证退订/新订阅时选择仍可复现。"""
    lines = ["# 单播选择序列（轮转，含成员变更）",
             "# 格式: seq selected | 备注",
             "# 初始订阅: s1 s2 s3 s4"]
    topic = Topic("t")
    for sid in ["s1", "s2", "s3", "s4"]:
        topic.subscribe(sid, lambda msg: None)
    notes = {
        5: "unsubscribe(s2) 在此之前执行：游标前移，s3 不跳过",
        9: "unsubscribe(s4) 在此之前执行：游标处退订，下一个是 s1",
        11: "subscribe(s5) 在此之前执行：新订阅者排到本轮队尾",
    }
    for i in range(1, 17):
        if i == 5:
            topic.unsubscribe("s2")
        if i == 9:
            topic.unsubscribe("s4")
        if i == 11:
            topic.subscribe("s5", lambda msg: None)
        selected = topic.unicast().delivered[0]
        note = f" | {notes[i]}" if i in notes else ""
        lines.append(f"{i:2d} {selected}{note}")
    return "\n".join(lines) + "\n"


def boundary_log():
    """订阅边界：边界点前后各发一次广播+单播，验证不漏投、不投给已退订者。"""
    lines = ["# 订阅边界投递结果",
             "# 格式: 事件 -> 实际投递"]
    topic = Topic("t")
    log = []
    for sid in ["a", "b"]:
        topic.subscribe(sid, lambda msg, s=sid: log.append((msg.seq, s)))
    lines.append(f"broadcast#1 (订阅者 a,b) -> {topic.broadcast().delivered}")
    lines.append(f"unicast#2  (游标在 a)   -> {topic.unicast().delivered}")
    lines.append("--- 边界点: subscribe(c) ---")
    lines.append(f"broadcast#3 (边界后须含 c) -> {topic.broadcast().delivered}")
    lines.append(f"unicast#4  (轮转继续)      -> {topic.unicast().delivered}")
    lines.append("--- 边界点: unsubscribe(a) ---")
    lines.append(f"broadcast#5 (边界后不得有 a) -> {topic.broadcast().delivered}")
    lines.append(f"unicast#6  (不得投给 a)      -> {topic.unicast().delivered}")
    lines.append("--- 边界点: unsubscribe(b), unsubscribe(c) ---")
    topic.unsubscribe("a")
    topic.broadcast()  # 上面已记录边界后第一次，这里补一次保持叙述简单
    lines.pop()  # 重新按真实顺序记录
    return lines, topic, log


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "selection_sequence.txt"), "w") as f:
        f.write(selection_sequence())

    # 边界日志：严格按时间顺序记录每次分发的实际投递
    topic = Topic("t")
    for sid in ["a", "b"]:
        topic.subscribe(sid, lambda msg: None)
    lines = ["# 订阅边界投递结果（按时间顺序）",
             "# 格式: seq 模式 投递对象 | 说明"]
    r = topic.broadcast(); lines.append(f"{r.message.seq} broadcast {r.delivered} | 订阅者 a,b")
    r = topic.unicast();  lines.append(f"{r.message.seq} unicast   {r.delivered} | 轮转从 a 开始")
    topic.subscribe("c", lambda msg: None)
    lines.append("# --- 边界点: subscribe(c) ---")
    r = topic.broadcast(); lines.append(f"{r.message.seq} broadcast {r.delivered} | 边界后必须包含 c")
    r = topic.unicast();  lines.append(f"{r.message.seq} unicast   {r.delivered} | 轮转继续，不受订阅影响")
    topic.unsubscribe("a")
    lines.append("# --- 边界点: unsubscribe(a) ---")
    r = topic.broadcast(); lines.append(f"{r.message.seq} broadcast {r.delivered} | 边界后不得再投给 a")
    r = topic.unicast();  lines.append(f"{r.message.seq} unicast   {r.delivered} | 不得投给已退订的 a")
    topic.unsubscribe("b"); topic.unsubscribe("c")
    lines.append("# --- 边界点: 全部退订 ---")
    r = topic.broadcast(); lines.append(f"{r.message.seq} broadcast {r.delivered} | 空订阅者：不投递不报错")
    r = topic.unicast();  lines.append(f"{r.message.seq} unicast   {r.delivered} | 空订阅者：单播丢弃")
    with open(os.path.join(OUT_DIR, "boundary_log.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("written: data/selection_sequence.txt, data/boundary_log.txt")


if __name__ == "__main__":
    main()
