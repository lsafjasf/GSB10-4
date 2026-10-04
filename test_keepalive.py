"""保活判定自测：python3 -m unittest -v test_keepalive.py

全部使用注入时间，无真实时钟、无随机性，结果可复现。
"""

import unittest

from keepalive import (
    Connection,
    Event,
    KeepaliveEngine,
    RESET_EVENTS,
    State,
    simulate,
)

IDLE = 30.0
IVAL = 5.0
N = 3
T_DEAD = IDLE + N * IVAL  # 45s


def run_script(engine, script, now0=0.0):
    """与 keepalive.simulate 相同，但返回可断言的完整 (state, probes) 序列。"""
    return simulate(engine, script, now0)


# ---------------------------------------------------------------------------
# 独立参考模型：用最直白的「自最后入站事件以来经过多久」规则实现，
# 与状态机引擎完全独立编写，用于对拍（对端不回包 / 回包两类场景）。
# ---------------------------------------------------------------------------
def reference(script, idle=IDLE, ival=IVAL, n=N, now0=0.0):
    last_inbound = now0
    got_inbound_since_deadline = False
    trace = []
    # 参考模型直接按「每个 tick 是否有入站事件」决定
    for t, ev in script:
        if ev in (Event.RX_DATA, Event.RX_PROBE_ACK):
            last_inbound = t
        silent_for = t - last_inbound
        if silent_for >= idle + n * ival:
            state = State.DEAD
            probes = n
        elif silent_for >= idle:
            probes = min(n, 1 + int((silent_for - idle) // ival))
            state = State.PROBING
        else:
            probes = 0
            state = State.ESTABLISHED
        trace.append((t, state, probes))
    return trace


class TestResetRules(unittest.TestCase):
    def test_reset_event_set_exact(self):
        """重置事件集合只能包含入站证据。"""
        self.assertEqual(RESET_EVENTS, frozenset({Event.RX_DATA, Event.RX_PROBE_ACK}))

    def test_tx_data_does_not_reset(self):
        """单向半开核心：只发不收，必须在固定时间被判死。"""
        eng = KeepaliveEngine(IDLE, IVAL, N)
        # 每秒发送一次数据（对端静默消失），持续 60s
        script = [(t, Event.TX_DATA) for t in range(61)]
        trace = run_script(eng, script)
        self.assertEqual(trace[int(IDLE)][1], State.PROBING)      # t=30 开始探测
        self.assertEqual(trace[int(T_DEAD)][1], State.DEAD)       # t=45 判死
        self.assertEqual(trace[60][1], State.DEAD)

    def test_rx_data_resets_idle(self):
        """有数据往来：对端在超时前有入站，连接永远健康。"""
        eng = KeepaliveEngine(IDLE, IVAL, N)
        script = []
        for t in range(121):
            ev = Event.RX_DATA if t % 29 == 0 else None
            script.append((float(t), ev))  # 入站间隔 29s < 30s
        trace = run_script(eng, script)
        self.assertTrue(all(s is State.ESTABLISHED for _, s, _ in trace))

    def test_ack_during_probing_restores(self):
        """探测中途收到应答：退回 ESTABLISHED 且探测计数清零。"""
        eng = KeepaliveEngine(IDLE, IVAL, N)
        conn = Connection(0.0)
        eng.tick(conn, 35.0)              # 30 发 #1, 35 发 #2
        self.assertIs(conn.state, State.PROBING)
        self.assertEqual(conn.probes_sent, 2)
        eng.on_event(conn, Event.RX_PROBE_ACK, 35.0)
        self.assertIs(conn.state, State.ESTABLISHED)
        self.assertEqual(conn.probes_sent, 0)
        # 35 重新活跃，之后再静默 30s 才重新探测
        eng.tick(conn, 64.0)
        self.assertIs(conn.state, State.ESTABLISHED)
        eng.tick(conn, 65.0)
        self.assertIs(conn.state, State.PROBING)
        self.assertEqual(conn.probes_sent, 1)

    def test_own_probe_does_not_reset(self):
        eng = KeepaliveEngine(IDLE, IVAL, N)
        conn = Connection(0.0)
        eng.on_event(conn, Event.TX_PROBE, 29.0)
        self.assertEqual(conn.last_activity, 0.0)
        eng.tick(conn, 29.0)
        self.assertIs(conn.state, State.ESTABLISHED)


class TestPureIdle(unittest.TestCase):
    def test_pure_idle_second_by_second(self):
        """纯空闲：逐秒状态序列必须完全确定。"""
        eng = KeepaliveEngine(IDLE, IVAL, N)
        trace = run_script(eng, [(float(t), None) for t in range(46)])
        expected = []
        for t in range(46):
            if t < 30:
                expected.append((float(t), State.ESTABLISHED, 0))
            elif t < 45:
                expected.append((float(t), State.PROBING, (t - 30) // 5 + 1))
            else:
                expected.append((float(t), State.DEAD, 3))
        self.assertEqual(trace, expected)

    def test_stays_dead(self):
        eng = KeepaliveEngine(IDLE, IVAL, N)
        conn = Connection(0.0)
        eng.tick(conn, 100.0)
        self.assertIs(conn.state, State.DEAD)
        eng.tick(conn, 200.0)
        self.assertIs(conn.state, State.DEAD)


class TestThresholdBoundary(unittest.TestCase):
    def test_idle_exactly_at_timeout(self):
        """恰好等于阈值（>= 左闭）：t=30 立即进入探测。"""
        eng = KeepaliveEngine(IDLE, IVAL, N)
        conn = Connection(0.0)
        eng.tick(conn, 29.999)
        self.assertIs(conn.state, State.ESTABLISHED)
        eng.tick(conn, 30.0)
        self.assertIs(conn.state, State.PROBING)
        self.assertEqual(conn.probes_sent, 1)

    def test_probe_intervals_exact(self):
        """探测正好卡在阈值：t=30/35/40 发探测，t=45 判死。"""
        eng = KeepaliveEngine(IDLE, IVAL, N)
        conn = Connection(0.0)
        for t, p in [(30.0, 1), (35.0, 2), (40.0, 3)]:
            eng.tick(conn, t)
            self.assertEqual(conn.probes_sent, p, f"t={t}")
            self.assertIs(conn.state, State.PROBING)
        eng.tick(conn, 45.0)
        self.assertIs(conn.state, State.DEAD)

    def test_ack_arrives_exactly_at_deadline(self):
        """应答与判死同一时刻：事件优先，连接存活（宁可晚杀，不可误杀）。"""
        eng = KeepaliveEngine(IDLE, IVAL, N)
        conn = Connection(0.0)
        eng.tick(conn, 40.0)               # 已发 3 个探测
        eng.on_event(conn, Event.RX_PROBE_ACK, 45.0)
        eng.tick(conn, 45.0)
        self.assertIs(conn.state, State.ESTABLISHED)

    def test_ack_one_instant_late(self):
        """迟一毫秒到的应答无效：45.001 已是 DEAD，不再复活。"""
        eng = KeepaliveEngine(IDLE, IVAL, N)
        conn = Connection(0.0)
        eng.tick(conn, 45.0)
        self.assertIs(conn.state, State.DEAD)
        eng.on_event(conn, Event.RX_PROBE_ACK, 45.001)
        self.assertIs(conn.state, State.DEAD)  # DEAD 为终态

    def test_ack_just_before_deadline(self):
        eng = KeepaliveEngine(IDLE, IVAL, N)
        conn = Connection(0.0)
        eng.on_event(conn, Event.RX_PROBE_ACK, 44.999)
        eng.tick(conn, 44.999)
        self.assertIs(conn.state, State.ESTABLISHED)

    def test_coarse_ticks_fast_forward(self):
        """稀疏 tick（跳跃时间）同样得到正确结果，不依赖逐秒轮询。"""
        eng = KeepaliveEngine(IDLE, IVAL, N)
        conn = Connection(0.0)
        eng.tick(conn, 44.999)
        self.assertIs(conn.state, State.PROBING)
        self.assertEqual(conn.probes_sent, 3)
        eng.tick(conn, 45.0)
        self.assertIs(conn.state, State.DEAD)


class TestHalfOpen(unittest.TestCase):
    def test_one_way_half_open_detected_in_n_rounds(self):
        """对端静默消失：idle_timeout 后 n 轮探测内必须识别。"""
        eng = KeepaliveEngine(IDLE, IVAL, N)
        conn = Connection(0.0)
        # 本端持续写、对端无任何入站
        t = 0.0
        while conn.state is not State.DEAD:
            t += 1.0
            eng.on_event(conn, Event.TX_DATA, t)
            eng.tick(conn, t)
        self.assertEqual(t, T_DEAD)
        self.assertEqual(conn.probes_sent, N)

    def test_responsive_peer_never_dies(self):
        """健康连接（对端正常回包）永不误判。"""
        eng = KeepaliveEngine(IDLE, IVAL, N)
        conn = Connection(0.0)
        for t in range(1, 1000):
            if t % 10 == 0:
                eng.on_event(conn, Event.RX_DATA, float(t))
            eng.tick(conn, float(t))
        self.assertIs(conn.state, State.ESTABLISHED)


class TestDifferentialAgainstReference(unittest.TestCase):
    """引擎 vs 独立参考模型对拍：静默与回包场景逐 tick 一致。"""

    def test_silent_peer(self):
        eng = KeepaliveEngine(IDLE, IVAL, N)
        script = [(float(t), None) for t in range(46)]
        self.assertEqual(run_script(eng, script), reference(script))

    def test_tx_only_silent_peer(self):
        eng = KeepaliveEngine(IDLE, IVAL, N)
        script = [(float(t), Event.TX_DATA) for t in range(46)]
        self.assertEqual(run_script(eng, script), reference(script))

    def test_bursty_rx(self):
        """随机化的突发入站模式（固定种子，可复现）。"""
        import random
        rng = random.Random(20261005)
        script = []
        for t in range(200):
            ev = Event.RX_DATA if rng.random() < 0.1 else None
            script.append((float(t), ev))
        eng = KeepaliveEngine(IDLE, IVAL, N)
        self.assertEqual(run_script(eng, script), reference(script))

    def test_ack_in_probing_phase(self):
        eng = KeepaliveEngine(IDLE, IVAL, N)
        script = [(float(t), None) for t in range(36)]
        script.append((36.0, Event.RX_PROBE_ACK))
        script += [(float(t), None) for t in range(37, 90)]
        # 参考模型需要处理 DEAD 终态语义：复活后重新计时；引擎在 36 回活，
        # 下一个判死时刻为 36+45=81。参考模型按最后入站重算，行为一致。
        self.assertEqual(run_script(eng, script), reference(script))


if __name__ == "__main__":
    unittest.main(verbosity=2)
