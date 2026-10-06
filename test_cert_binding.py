#!/usr/bin/env python3
"""会话-证书绑定校验自测（仅标准库 unittest）。

运行: python3 -m unittest test_cert_binding -v
"""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from cert_binding import (
    BindingIdentity,
    CertFormatError,
    SessionBindingStore,
    VerifyStatus,
    parse_certificate,
    pem_to_der,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def load_identity(name: str) -> BindingIdentity:
    with open(os.path.join(FIXTURES, name + ".pem")) as fh:
        return BindingIdentity.from_pem(fh.read())


def load_expected() -> dict:
    with open(os.path.join(FIXTURES, "expected.json")) as fh:
        return json.load(fh)


class CrossCheckTest(unittest.TestCase):
    """对拍：库解析结果必须与 openssl 独立计算的数据逐条一致。"""

    @classmethod
    def setUpClass(cls):
        cls.expected = load_expected()

    def test_all_fixtures_match_openssl(self):
        for name, exp in self.expected.items():
            with self.subTest(cert=name):
                ident = load_identity(name)
                self.assertEqual(ident.cert_fp, exp["cert_fp"], "整证书指纹不一致")
                self.assertEqual(ident.spki_fp, exp["spki_fp"], "公钥指纹不一致")
                self.assertEqual(ident.subject_fp, exp["subject_fp"], "主体指纹不一致")
                self.assertEqual(ident.serial, exp["serial"], "序列号不一致")
                self.assertEqual(
                    ident.not_before.isoformat(), exp["not_before"], "notBefore 不一致"
                )
                self.assertEqual(
                    ident.not_after.isoformat(), exp["not_after"], "notAfter 不一致"
                )

    def test_renewed_cert_same_subject_different_key(self):
        """续期证书：主体指纹必须相同，公钥/整证书指纹必须不同。"""
        old = load_identity("alice")
        new = load_identity("alice_renewed")
        self.assertEqual(old.subject_fp, new.subject_fp)
        self.assertNotEqual(old.spki_fp, new.spki_fp)
        self.assertNotEqual(old.cert_fp, new.cert_fp)
        self.assertNotEqual(old.serial, new.serial)


class BindingFlowTest(unittest.TestCase):
    """绑定规则主流程：首次连接 / 正常重连 / 换证书 / 吊销 / 过期。"""

    def setUp(self):
        self.store = SessionBindingStore()
        self.alice = load_identity("alice")
        self.alice_renewed = load_identity("alice_renewed")
        self.bob = load_identity("bob")
        self.revoked = load_identity("revoked")
        self.expired = load_identity("expired")

    def test_first_connect_binds(self):
        r = self.store.verify("s1", self.alice, now=NOW)
        self.assertEqual(r.status, VerifyStatus.BOUND)
        self.assertTrue(r.ok)
        self.assertTrue(r.session_alive)
        self.assertTrue(self.store.is_bound("s1"))

    def test_normal_reconnect_ok(self):
        self.store.verify("s1", self.alice, now=NOW)
        r = self.store.verify("s1", self.alice, now=NOW)
        self.assertEqual(r.status, VerifyStatus.OK)
        self.assertTrue(r.ok)
        self.assertIn("一致", r.reason)
        self.assertTrue(self.store.is_bound("s1"))

    def test_reconnect_with_different_cert_rejected(self):
        self.store.verify("s1", self.alice, now=NOW)
        r = self.store.verify("s1", self.bob, now=NOW)
        self.assertEqual(r.status, VerifyStatus.REJECT_CHANGED)
        self.assertFalse(r.ok)
        self.assertFalse(r.session_alive)
        self.assertIn("主体指纹", r.reason)
        self.assertIn("公钥指纹", r.reason)
        self.assertIn("证书指纹", r.reason)
        # 旧会话必须作废，不得保留绑定
        self.assertFalse(self.store.is_bound("s1"))

    def test_reconnect_with_renewed_cert_rejected(self):
        """同主体续期证书也算换证书：必须拒绝，不得因主体相同而放行。"""
        self.store.verify("s1", self.alice, now=NOW)
        r = self.store.verify("s1", self.alice_renewed, now=NOW)
        self.assertEqual(r.status, VerifyStatus.REJECT_CHANGED)
        self.assertFalse(r.ok)
        self.assertNotIn("主体指纹", r.reason)  # 主体相同
        self.assertIn("公钥指纹", r.reason)
        self.assertIn("证书指纹", r.reason)
        self.assertFalse(self.store.is_bound("s1"))

    def test_no_downgrade_after_reject(self):
        """拒绝后不得降级放行：会话已死，任何后续连接都是全新绑定流程。"""
        self.store.verify("s1", self.alice, now=NOW)
        r = self.store.verify("s1", self.bob, now=NOW)
        self.assertFalse(r.ok)
        self.assertFalse(self.store.is_bound("s1"))
        # 攻击者换回 alice 原证书也无法"恢复"旧会话，只能重新绑定
        r2 = self.store.verify("s1", self.alice, now=NOW)
        self.assertEqual(r2.status, VerifyStatus.BOUND)

    def test_revoked_cert_rejected(self):
        self.store.verify("s1", self.revoked, now=NOW)
        self.store.revoke(serial=self.revoked.serial)
        r = self.store.verify("s1", self.revoked, now=NOW)
        self.assertEqual(r.status, VerifyStatus.REJECT_REVOKED)
        self.assertFalse(r.ok)
        self.assertFalse(r.session_alive)
        self.assertFalse(self.store.is_bound("s1"))

    def test_revoked_cert_rejected_on_first_connect(self):
        """吊销检查先于绑定：首次连接出示已吊销证书同样拒绝。"""
        self.store.revoke(serial=self.revoked.serial)
        r = self.store.verify("s-new", self.revoked, now=NOW)
        self.assertEqual(r.status, VerifyStatus.REJECT_REVOKED)
        self.assertFalse(self.store.is_bound("s-new"))

    def test_revoke_by_cert_fingerprint(self):
        self.store.verify("s1", self.revoked, now=NOW)
        self.store.revoke(cert_fp=self.revoked.cert_fp)
        r = self.store.verify("s1", self.revoked, now=NOW)
        self.assertEqual(r.status, VerifyStatus.REJECT_REVOKED)

    def test_expired_cert_rejected_on_first_connect(self):
        r = self.store.verify("s1", self.expired, now=NOW)
        self.assertEqual(r.status, VerifyStatus.REJECT_EXPIRED)
        self.assertFalse(r.ok)
        self.assertFalse(self.store.is_bound("s1"))

    def test_cert_expires_after_binding(self):
        """绑定后证书过期：重连必须拒绝并作废旧会话。"""
        self.store.verify("s1", self.alice, now=NOW)
        later = self.alice.not_after + timedelta(seconds=1)
        r = self.store.verify("s1", self.alice, now=later)
        self.assertEqual(r.status, VerifyStatus.REJECT_EXPIRED)
        self.assertFalse(self.store.is_bound("s1"))

    def test_expired_check_beats_revoked(self):
        """优先级确定：既过期又吊销 -> 报过期。"""
        self.store.revoke(serial=self.expired.serial)
        r = self.store.verify("s1", self.expired, now=NOW)
        self.assertEqual(r.status, VerifyStatus.REJECT_EXPIRED)

    def test_changed_check_beats_nothing_when_valid(self):
        """换了证书但新证书本身有效 -> 报换证书（而非其他）。"""
        self.store.verify("s1", self.alice, now=NOW)
        r = self.store.verify("s1", self.bob, now=NOW)
        self.assertEqual(r.status, VerifyStatus.REJECT_CHANGED)

    def test_sessions_are_independent(self):
        self.store.verify("s1", self.alice, now=NOW)
        self.store.verify("s2", self.bob, now=NOW)
        self.assertEqual(self.store.verify("s1", self.alice, now=NOW).status, VerifyStatus.OK)
        self.assertEqual(self.store.verify("s2", self.bob, now=NOW).status, VerifyStatus.OK)
        # 同一张证书可以绑到不同会话（绑定方向是 会话->证书）
        self.assertEqual(self.store.verify("s3", self.alice, now=NOW).status, VerifyStatus.BOUND)

    def test_unbind(self):
        self.store.verify("s1", self.alice, now=NOW)
        self.assertTrue(self.store.unbind("s1"))
        self.assertFalse(self.store.unbind("s1"))
        self.assertFalse(self.store.is_bound("s1"))


class EdgeCaseTest(unittest.TestCase):
    """边界用例。"""

    def test_malformed_der_rejected(self):
        with self.assertRaises(CertFormatError):
            parse_certificate(b"\x30\x03\x02\x01")  # 截断
        with self.assertRaises(CertFormatError):
            parse_certificate(b"not a der at all")
        with self.assertRaises(CertFormatError):
            parse_certificate(b"")

    def test_malformed_pem_rejected(self):
        with self.assertRaises(CertFormatError):
            pem_to_der("hello world")
        with self.assertRaises(CertFormatError):
            pem_to_der("-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----")
        with self.assertRaises(CertFormatError):
            pem_to_der("-----BEGIN CERTIFICATE-----\n!!!not-base64!!!\n-----END CERTIFICATE-----")

    def test_generalized_time_not_after(self):
        """longlived 证书 notAfter 在 2050 年之后，必须是 GeneralizedTime 且解析正确。"""
        ident = load_identity("longlived")
        exp = load_expected()["longlived"]
        self.assertGreater(ident.not_after.year, 2050)
        self.assertEqual(ident.not_after.isoformat(), exp["not_after"])

    def test_utctime_not_after(self):
        """expired 证书有效期在 2001-2002，走 UTCTime 编码。"""
        ident = load_identity("expired")
        self.assertEqual(ident.not_after.year, 2002)

    def test_verify_at_exact_not_after_is_valid(self):
        """now == notAfter 边界：尚未超过有效期，放行。"""
        ident = load_identity("alice")
        store = SessionBindingStore()
        r = store.verify("s1", ident, now=ident.not_after)
        self.assertEqual(r.status, VerifyStatus.BOUND)

    def test_same_session_id_rebind_after_invalidation(self):
        """会话作废后同 id 可重新绑定（视为全新会话生命周期）。"""
        store = SessionBindingStore()
        alice = load_identity("alice")
        bob = load_identity("bob")
        store.verify("s1", alice, now=NOW)
        store.verify("s1", bob, now=NOW)  # 拒绝并作废
        r = store.verify("s1", bob, now=NOW)  # 全新绑定
        self.assertEqual(r.status, VerifyStatus.BOUND)
        self.assertEqual(store.verify("s1", bob, now=NOW).status, VerifyStatus.OK)


if __name__ == "__main__":
    unittest.main()
