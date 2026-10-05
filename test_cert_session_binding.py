"""cert_session_binding 自测（python3 -m unittest -v test_cert_session_binding）。

对拍数据说明：
  下面的 GOLDEN_* 常量是用与本实现无关的工具链(coreutils sha256sum)手工算出的，
  例如：
    spki_fp_A = printf '\x01\x02\x03\x04' | sha256sum
    binding_A = printf 'SESSBIND/v1\nCN=alice,O=Example\n<spki_fp_A>' | sha256sum
  测试逐条把库输出与这些手工值比对，保证绑定算法与人工核对一致。
"""

import unittest

from cert_session_binding import (
    PeerCertificate,
    SessionBinder,
    Verdict,
    binding_id_for,
)

# ---- 由 coreutils sha256sum 独立手工计算的黄金值 ----
SPKI_A = b"\x01\x02\x03\x04"
SPKI_B = b"\x05\x06\x07\x08"
GOLDEN_SPKI_FP_A = "9f64a747e1b97f131fabb6b447296c9b6f0201e79fb3c5356e6c77e89b6a806a"
GOLDEN_SPKI_FP_B = "55e5509f8052998294266ee5b50cb592938191fb5d67f73cac2e60b0276b1bdd"
SUBJ_ALICE = "CN=alice,O=Example"
SUBJ_MALLORY = "CN=mallory,O=Example"
GOLDEN_BIND_A = "e2f5edc26d4d59e4a7e9e03cbb9d0ebc9d2730ea4f6d70638fb13fede14c32d8"
GOLDEN_BIND_B = "740efc5ec96b309255d9a4375d9652ad82d1bd88fcf624feffcb470d28a97ff8"
GOLDEN_BIND_C = "aa86dc95bdbadabf74a2f1c21f5e0dcd5a2d5a8d39bca3866a5817fc9dac9c1c"

T0 = 1_000_000.0  # 固定时钟基准，保证结果确定性


def make_cert(subject=SUBJ_ALICE, spki=SPKI_A, not_after=T0 + 3600, serial=1001):
    return PeerCertificate(subject, spki, not_after, serial)


class GoldenVectorTest(unittest.TestCase):
    def test_spki_fingerprints_match_manual_sha256sum(self):
        self.assertEqual(make_cert(spki=SPKI_A).spki_fingerprint, GOLDEN_SPKI_FP_A)
        self.assertEqual(make_cert(spki=SPKI_B).spki_fingerprint, GOLDEN_SPKI_FP_B)

    def test_binding_ids_match_manual_sha256sum(self):
        cert_a = make_cert(SUBJ_ALICE, SPKI_A, serial=1001)
        cert_b = make_cert(SUBJ_ALICE, SPKI_B, serial=1002)
        cert_c = make_cert(SUBJ_MALLORY, SPKI_A, serial=1003)
        self.assertEqual(binding_id_for(cert_a), GOLDEN_BIND_A)
        self.assertEqual(binding_id_for(cert_b), GOLDEN_BIND_B)
        self.assertEqual(binding_id_for(cert_c), GOLDEN_BIND_C)

    def test_subject_or_pubkey_change_breaks_binding(self):
        cert_a = make_cert(SUBJ_ALICE, SPKI_A)
        cert_b = make_cert(SUBJ_ALICE, SPKI_B)   # 同主体、换公钥
        cert_c = make_cert(SUBJ_MALLORY, SPKI_A)  # 同公钥、换主体
        bid_a = binding_id_for(cert_a)
        self.assertNotEqual(binding_id_for(cert_b), bid_a)
        self.assertNotEqual(binding_id_for(cert_c), bid_a)


class BindingFlowTest(unittest.TestCase):
    def setUp(self):
        self.clock = T0
        self.binder = SessionBinder(now=lambda: self.clock)
        self.cert = make_cert()

    # 1. 首次连接
    def test_first_connect_creates_binding(self):
        d = self.binder.validate("sess-1", self.cert)
        self.assertEqual(d.verdict, Verdict.ACCEPT_NEW)
        self.assertTrue(d.accepted)
        self.assertTrue(self.binder.has_session("sess-1"))
        self.assertEqual(
            self.binder.session_binding("sess-1"),
            (GOLDEN_BIND_A, SUBJ_ALICE, GOLDEN_SPKI_FP_A),
        )

    # 2. 正常重连
    def test_normal_reconnect_resumes(self):
        self.binder.validate("sess-1", self.cert)
        d = self.binder.validate("sess-1", self.cert)
        self.assertEqual(d.verdict, Verdict.ACCEPT_RESUME)
        self.assertTrue(d.accepted)
        self.assertFalse(d.session_invalidated)

    # 3a. 换证书重连：只换公钥
    def test_reconnect_with_new_key_rejected_and_invalidated(self):
        self.binder.validate("sess-1", self.cert)
        new_cert = make_cert(spki=SPKI_B, serial=1002)
        d = self.binder.validate("sess-1", new_cert)
        self.assertEqual(d.verdict, Verdict.REJECT_CERT_CHANGED)
        self.assertFalse(d.accepted)
        self.assertTrue(d.session_invalidated)
        self.assertFalse(self.binder.has_session("sess-1"))  # 旧会话已作废
        self.assertIn("证书更换", d.reason)

    # 3b. 换证书重连：只换主体（公钥相同也必须判不匹配）
    def test_reconnect_with_new_subject_rejected(self):
        self.binder.validate("sess-1", self.cert)
        other = make_cert(subject=SUBJ_MALLORY, serial=1003)
        d = self.binder.validate("sess-1", other)
        self.assertEqual(d.verdict, Verdict.REJECT_CERT_CHANGED)
        self.assertFalse(d.accepted)
        self.assertTrue(d.session_invalidated)

    # 4. 证书被吊销（先建立会话，之后吊销，再重连）
    def test_revoked_cert_rejected_and_session_killed(self):
        self.binder.validate("sess-1", self.cert)
        self.binder.revoke(1001)
        d = self.binder.validate("sess-1", self.cert)
        self.assertEqual(d.verdict, Verdict.REJECT_CERT_REVOKED)
        self.assertFalse(d.accepted)
        self.assertTrue(d.session_invalidated)
        self.assertFalse(self.binder.has_session("sess-1"))
        self.assertIn("吊销", d.reason)

    # 4b. 证书首次连接即被吊销
    def test_first_connect_with_revoked_cert_rejected(self):
        binder = SessionBinder(revoked_serials={1001}, now=lambda: self.clock)
        d = binder.validate("sess-9", self.cert)
        self.assertEqual(d.verdict, Verdict.REJECT_CERT_REVOKED)
        self.assertFalse(d.accepted)
        self.assertFalse(binder.has_session("sess-9"))

    # 5. 证书过期
    def test_expired_cert_rejected_and_session_killed(self):
        self.binder.validate("sess-1", self.cert)
        self.clock = T0 + 3601  # 超过 not_after
        d = self.binder.validate("sess-1", self.cert)
        self.assertEqual(d.verdict, Verdict.REJECT_CERT_EXPIRED)
        self.assertFalse(d.accepted)
        self.assertTrue(d.session_invalidated)
        self.assertFalse(self.binder.has_session("sess-1"))

    # 5b. not_after 恰等于当前时刻：按过期处理（边界）
    def test_not_after_equals_now_is_expired(self):
        cert = make_cert(not_after=T0)
        d = self.binder.validate("sess-2", cert)
        self.assertEqual(d.verdict, Verdict.REJECT_CERT_EXPIRED)
        self.assertFalse(d.accepted)

    # 6. 拒绝不得降级：所有 REJECT_* 的 accepted 必须为 False
    def test_rejections_never_downgrade(self):
        self.binder.validate("sess-1", self.cert)
        cases = [
            make_cert(spki=SPKI_B, serial=1002),                      # 换证书
            make_cert(not_after=T0 - 1),                             # 过期
        ]
        self.binder.revoke(1001)
        cases.append(make_cert())                                    # 吊销
        for cert in cases:
            d = self.binder.validate("sess-1", cert)
            self.assertFalse(d.accepted, f"{d.verdict} 不得降级放行")
            self.assertIn("REJECT", d.verdict.value)

    # 7. 不同会话互不影响；会话编号不参与认证
    def test_sessions_are_independent(self):
        self.binder.validate("sess-1", self.cert)
        self.binder.validate("sess-2", self.cert)
        d = self.binder.validate("sess-1", make_cert(spki=SPKI_B, serial=1002))
        self.assertEqual(d.verdict, Verdict.REJECT_CERT_CHANGED)
        self.assertFalse(self.binder.has_session("sess-1"))
        self.assertTrue(self.binder.has_session("sess-2"))  # 另一会话不受牵连
        d2 = self.binder.validate("sess-2", self.cert)
        self.assertEqual(d2.verdict, Verdict.ACCEPT_RESUME)

    # 8. 旧会话作废后，重新完整认证可建立新绑定
    def test_full_reauth_after_invalidation(self):
        self.binder.validate("sess-1", self.cert)
        self.binder.validate("sess-1", make_cert(spki=SPKI_B, serial=1002))
        d = self.binder.validate("sess-1", make_cert(spki=SPKI_B, serial=1002))
        self.assertEqual(d.verdict, Verdict.ACCEPT_NEW)
        self.assertEqual(
            self.binder.session_binding("sess-1")[0], GOLDEN_BIND_B
        )


# ---- 结果对照表对拍：逐条手工核对 ----
# 每行 = (用例, 操作序列期望, 最终判定, 会话是否存活)
EXPECTED_TABLE = [
    ("首次连接(有效证书)",            [(Verdict.ACCEPT_NEW,    True)],  True),
    ("正常重连(同一证书)",            [(Verdict.ACCEPT_NEW,    True),
                                      (Verdict.ACCEPT_RESUME,  True)], True),
    ("换公钥重连",                    [(Verdict.ACCEPT_NEW,    True),
                                      (Verdict.REJECT_CERT_CHANGED, False)], False),
    ("换主体重连",                    [(Verdict.ACCEPT_NEW,    True),
                                      (Verdict.REJECT_CERT_CHANGED, False)], False),
    ("证书被吊销后重连",              [(Verdict.ACCEPT_NEW,    True),
                                      (Verdict.REJECT_CERT_REVOKED, False)], False),
    ("证书过期后重连",                [(Verdict.ACCEPT_NEW,    True),
                                      (Verdict.REJECT_CERT_EXPIRED, False)], False),
    ("首次连接即吊销",                [(Verdict.REJECT_CERT_REVOKED, False)], False),
]


class CrossCheckTableTest(unittest.TestCase):
    def test_table_matches_manual_expectations(self):
        rows = []
        for name, steps, alive_after in EXPECTED_TABLE:
            clock = T0
            binder = SessionBinder(now=lambda: clock)
            sid = "sess-x"
            last = None
            for idx, (expected_verdict, expected_accept) in enumerate(steps):
                cert = make_cert()
                if "换公钥" in name and idx == 1:
                    cert = make_cert(spki=SPKI_B, serial=1002)
                if "换主体" in name and idx == 1:
                    cert = make_cert(subject=SUBJ_MALLORY, serial=1003)
                if "吊销" in name and idx == len(steps) - 1:
                    binder.revoke(1001)
                if "过期" in name and idx == len(steps) - 1:
                    clock = T0 + 9999
                last = binder.validate(sid, cert)
                self.assertEqual(last.verdict, expected_verdict, name)
                self.assertEqual(last.accepted, expected_accept, name)
            self.assertEqual(binder.has_session(sid), alive_after, name)
            rows.append((name, last.verdict.value, "存活" if alive_after else "已作废"))

        # 打印对照表，便于和文档逐条核对
        print("\n%-22s | %-22s | %s" % ("用例", "最终判定", "会话状态"))
        print("-" * 60)
        for name, verdict, state in rows:
            print("%-22s | %-22s | %s" % (name, verdict, state))


if __name__ == "__main__":
    unittest.main(verbosity=2)
