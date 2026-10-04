"""对拍：随机操作剧本下，库的分发结果必须与独立参考实现、
以及逐条手工投递的期望值完全一致。

用法：python3 cross_check.py [--trials N] [--seed S]
"""

import argparse
import random

from dispatcher import Topic
from reference_model import ReferenceTopic


def run_script(script):
    """同一剧本在库与参考实现上各跑一遍，返回 (库投递记录, 参考投递记录)。"""
    topic = Topic("t")
    ref = ReferenceTopic("t")
    lib_log, ref_log = [], []
    for op in script:
        kind = op[0]
        if kind == "sub":
            sid = op[1]
            topic.subscribe(sid, lambda msg, s=sid: lib_log.append((msg.seq, s)))
            ref.subscribe(sid)
        elif kind == "unsub":
            sid = op[1]
            assert topic.unsubscribe(sid) == ref.unsubscribe(sid)
        else:  # dispatch
            result = topic.broadcast() if kind == "broadcast" else topic.unicast()
            expected = ref.dispatch(kind)
            assert result.delivered == expected, (
                f"seq={result.message.seq} {kind}: lib={result.delivered} ref={expected}")
            for sid in expected:
                ref_log.append((result.message.seq, sid))
    return lib_log, ref_log


def gen_script(rng, length):
    """生成随机剧本：订阅/退订/广播/单播混合。同一 id 退订后不再重订阅。"""
    script = []
    pool = [f"s{i}" for i in range(12)]
    alive, dead = [], list(pool)
    for _ in range(length):
        choices = ["broadcast", "unicast"]
        if dead:
            choices += ["sub"] * 2
        if alive:
            choices += ["unsub"] * 2
        op = rng.choice(choices)
        if op == "sub":
            sid = dead.pop(rng.randrange(len(dead)))
            alive.append(sid)
            script.append(("sub", sid))
        elif op == "unsub":
            sid = alive.pop(rng.randrange(len(alive)))
            dead.append(sid)
            script.append(("unsub", sid))
        else:
            script.append((op,))
    return script


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20261005)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    total_dispatches = 0
    for trial in range(args.trials):
        script = gen_script(rng, length=rng.randint(20, 120))
        lib_log, ref_log = run_script(script)
        assert lib_log == ref_log, f"trial {trial}: log mismatch"
        total_dispatches += sum(1 for op in script if op[0] in ("broadcast", "unicast"))
    print(f"OK: {args.trials} trials, {total_dispatches} dispatches, "
          f"lib log == reference log (seed={args.seed})")


if __name__ == "__main__":
    main()
