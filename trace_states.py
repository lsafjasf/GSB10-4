"""连接级状态序列演示：打印每条连接的状态迁移，验证空闲判定与在途结清。"""
from watermark_pool import WatermarkPool


class VirtualClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def main():
    clock = VirtualClock()
    pool = WatermarkPool(min_size=2, max_size=4, low_water=0.3, high_water=0.7,
                         expand_step=2, shrink_step=1, max_shrink_ratio=1.0,
                         idle_timeout=5.0, cooldown=1.0, time_fn=clock)
    prev = {}

    def snap():
        return {cid: (c.state.value, c.in_flight) for cid, c in pool._conns.items()}

    def show(action):
        nonlocal prev
        cur = snap()
        for cid in sorted(set(cur) | set(prev)):
            before = prev.get(cid, ("-", 0))
            after = cur.get(cid, ("CLOSED", 0))
            if before != after:
                print("t=%4.1f  conn#%-2d %-8s -> %-8s in_flight:%d->%d  [%s]"
                      % (clock.t, cid, before[0], after[0], before[1],
                         after[1], action))
        prev = cur

    show("预热 min_size=2")
    c0 = pool.try_acquire(); show("acquire #0")
    c1 = pool.try_acquire(); show("acquire #1")
    pool.drain(c1); show("drain #1: 在途->DRAINING, 先补位 #2 再摘除")
    clock.advance(5.0)
    pool.tick(); show("tick: #1 在途不得关闭; util=1/3>0.3 滞回不缩")
    pool.release(c1); show("release #1: 长尾结清, DRAINING 关闭")
    pool.release(c0); show("release #0")
    # 触发一次扩容，再造出可缩的空闲连接
    a = pool.try_acquire(); b = pool.try_acquire()
    assert pool.try_acquire() is None
    clock.advance(1.1)
    pool.tick(); show("tick: util=1.0>=0.7 触发扩容 (+2)")
    c = pool.try_acquire(); d = pool.try_acquire()
    for cid in (a, b, c, d):
        pool.release(cid)
    show("release 全部 4 条")
    clock.advance(6.0)
    pool.tick(); show("tick: util=0<=0.3, 空闲>=5s, 缩容关闭 1 条")
    clock.advance(1.1)
    pool.tick(); show("tick: 继续缩容, 回落到 min_size=2")
    pool.check_invariants()
    print("不变量校验通过; stats:",
          {k: v for k, v in pool.stats.items() if v})


if __name__ == "__main__":
    main()
