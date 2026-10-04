"""保活判定自测。运行：python3 test_keepalive.py -v

统一参数：T=60（空闲阈值）, I=10（探测间隔）, C=3（探测次数）
=> 探测时刻 last+60 / last+70 / last+80，断开阈值 last+90。
"""

import unittest

from keepalive import KeepAliveMonitor, ALIVE, DEAD

T, I, C = 60, 10, 3


def make():
    m = KeepAliveMonitor(idle_timeout=T, probe_interval=I, probe_count=C)
    m.add("c1", t=0)
    return m


class TestResetRules(unittest.TestCase):
    def test_data_traffic_resets_idle(self):
        """有数据往来：每个方向的数据都重置计时，永不进入探测。"""
        m = make()
        for k in range(1, 20):
            t = k * 50  # 每 50s 一次活动，始终 < T=60
            if k % 2:
                m.send_data("c1", 100, t)
            else:
                m.recv_data("c1", 100, t)
            self.assertEqual(m.advance(t), [])
        self.assertEqual(m.state("c1"), ALIVE)
        self.assertEqual(m.advance(1010), [("probe", "c1", 1, 950 + T)])

    def test_ack_does_not_reset(self):
        """纯 ACK / 保活应答不重置：对端只回 ACK 不发数据，照样判死。"""
        m = make()
        for t in range(0, 200, 5):
            m.recv_ack("c1", t)  # 大量 ACK，全部无效
        events = m.advance(200)
        kinds = [(e[0], e[1], e[2]) if e[0] == "probe" else (e[0], e[1])
                 for e in events]
        self.assertEqual(kinds,
                         [("probe", "c1", 1), ("probe", "c1", 2),
                          ("probe", "c1", 3), ("dead", "c1")])
        self.assertEqual(events[-1][-1], T + C * I)

    def test_data_during_probing_resets_probe_count(self):
        """探测期间收到数据：探测计数清零，重新按完整阈值计时。"""
        m = make()
        e1 = m.advance(65)  # 已发 1 次探测
        self.assertEqual(e1, [("probe", "c1", 1, 60)])
        m.recv_data("c1", 1, 65)  # 重置
        events = m.advance(65 + T + C * I)
        kinds = [(e[0], e[1], e[2]) if e[0] == "probe" else (e[0], e[1])
                 for e in events]
        self.assertEqual(kinds,
                         [("probe", "c1", 1), ("probe", "c1", 2),
                          ("probe", "c1", 3), ("dead", "c1")])
        self.assertEqual(events[-1][-1], 65 + 90)


class TestScenarios(unittest.TestCase):
    def test_pure_idle_full_sequence(self):
        """纯空闲：完整可复现判定序列。"""
        m = make()
        events = m.advance(1000)
        self.assertEqual(events, [
            ("probe", "c1", 1, 60.0),
            ("probe", "c1", 2, 70.0),
            ("probe", "c1", 3, 80.0),
            ("dead", "c1", 90.0),
        ])
        self.assertEqual(m.state("c1"), DEAD)

    def test_half_open_detected_within_rounds(self):
        """单向半开：对端静默消失，必须在 C 轮探测内识别。

        场景：t=100 最后一次收到数据后对端消失（不发包、不回 ACK）。
        期望：t=190 (= 100+T+C*I) 判死，即最后活动后 C 轮探测内。
        """
        m = make()
        m.recv_data("c1", 512, 100)
        m.advance(100)
        self.assertEqual(m.state("c1"), ALIVE)
        deadline = m.disconnect_threshold("c1")
        self.assertEqual(deadline, 100 + T + C * I)
        events = m.advance(deadline)
        self.assertEqual(events[-1], ("dead", "c1", 190.0))
        self.assertEqual(m.state("c1"), DEAD)
        # 识别耗时 = T + C*I = 90s，探测轮数恰为 C=3
        probes = [e for e in events if e[0] == "probe"]
        self.assertEqual(len(probes), C)

    def test_probe_exactly_at_threshold(self):
        """探测正好卡在阈值：t = last+T+C*I 当拍即判死；早 1 秒则仍存活。"""
        m = make()
        events = m.advance(89)  # 阈值 90 前一秒
        self.assertEqual([e[0] for e in events], ["probe", "probe", "probe"])
        self.assertEqual(m.state("c1"), ALIVE)
        self.assertEqual(m.advance(90), [("dead", "c1", 90.0)])
        self.assertEqual(m.state("c1"), DEAD)

    def test_activity_one_tick_before_threshold_saves_connection(self):
        """阈值前一拍来数据：连接救回，且后续按新时刻重新计时。"""
        m = make()
        m.advance(80)
        m.recv_data("c1", 1, 89.5)  # 距判死 0.5s
        self.assertEqual(m.advance(90), [])
        self.assertEqual(m.state("c1"), ALIVE)
        self.assertEqual(m.disconnect_threshold("c1"), 89.5 + 90)

    def test_idle_boundary_exactly_T(self):
        """t-last == T 当拍进入探测（>= 语义），T-1 时不探测。"""
        m = make()
        self.assertEqual(m.advance(59), [])
        self.assertEqual(m.advance(60), [("probe", "c1", 1, 60.0)])


class TestDeterminismAndMultiConn(unittest.TestCase):
    def test_reproducible_sequence(self):
        """同一事件序列重放两次，判定序列逐字节一致。"""
        def run():
            m = make()
            out = []
            m.send_data("c1", 10, 30)
            out += m.advance(75)
            m.recv_data("c1", 10, 75)
            out += m.advance(300)
            return out
        self.assertEqual(run(), run())

    def test_many_connections_independent(self):
        """多连接互不影响；事件按 (时刻, conn_id) 确定性排序。"""
        m = KeepAliveMonitor(T, I, C)
        for i in range(100):
            m.add(i, t=0)
        m.recv_data(7, 1, 30)  # 7 号连接重置过
        events = m.advance(90)
        dead = sorted(e[1] for e in events if e[0] == "dead")
        self.assertEqual(dead, [i for i in range(100) if i != 7])
        self.assertEqual(m.state(7), ALIVE)
        self.assertEqual(events, sorted(events, key=lambda e: (e[-1], e[1])))

    def test_remove_and_dead_conn_no_more_events(self):
        m = make()
        m.advance(100)  # 判死
        self.assertEqual(m.advance(10_000), [])  # 死后不再产生事件
        m.remove("c1")
        self.assertEqual(m.advance(20_000), [])


if __name__ == "__main__":
    unittest.main()
