"""会话-证书绑定校验库（仅标准库）。

绑定标识 binding_id 的选取：
    binding_id = SHA-256( "SESSBIND/v1\\n" || subject_dn || "\\n" || spki_sha256_hex )

其中：
  - subject_dn       证书主体 DN（覆盖证书主体身份）；
  - spki_sha256_hex  证书 SubjectPublicKeyInfo(DER) 的 SHA-256 十六进制串
                     （覆盖公钥指纹；换证书即换公钥，指纹必变）。

因此绑定同时覆盖「证书主体」与「公钥指纹」，而非只绑会话编号。
会话编号(session_id)仅作为查找键，不参与任何认证决策。

判定顺序（确定性，先证书有效性、后绑定一致性）：
  1. 过期检查   : not_after <= now            -> REJECT_CERT_EXPIRED
  2. 吊销检查   : serial in 吊销列表           -> REJECT_CERT_REVOKED
  3. 绑定检查   : 无旧会话 -> ACCEPT_NEW 并建立绑定；
                  一致     -> ACCEPT_RESUME；
                  不一致   -> REJECT_CERT_CHANGED 并作废旧会话。
  任何针对已存在会话的拒绝判定都会作废旧会话（fail-closed），
  绝不降级为单向认证放行。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Iterable, Optional, Tuple

_BINDING_DOMAIN = b"SESSBIND/v1\n"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PeerCertificate:
    """对端证书中参与绑定决策的字段（生产环境从 TLS 握手对端证书解析得到）。"""

    subject_dn: str   # 证书主体 DN，如 "CN=alice,O=Example"
    spki: bytes       # SubjectPublicKeyInfo 的 DER 编码（公钥）
    not_after: float  # 证书有效期截止（Unix 时间戳，秒）
    serial: int       # 证书序列号（吊销列表按序列号判定）

    @property
    def spki_fingerprint(self) -> str:
        """公钥指纹：SPKI DER 的 SHA-256 十六进制。"""
        return _sha256_hex(self.spki)


def binding_id_for(cert: PeerCertificate) -> str:
    """计算绑定标识：覆盖证书主体 DN 与公钥指纹。"""
    h = hashlib.sha256()
    h.update(_BINDING_DOMAIN)
    h.update(cert.subject_dn.encode("utf-8"))
    h.update(b"\n")
    h.update(cert.spki_fingerprint.encode("ascii"))
    return h.hexdigest()


class Verdict(Enum):
    ACCEPT_NEW = "ACCEPT_NEW"                    # 首次连接，建立绑定
    ACCEPT_RESUME = "ACCEPT_RESUME"              # 重连且绑定一致
    REJECT_CERT_CHANGED = "REJECT_CERT_CHANGED"  # 换证书重连，旧会话作废
    REJECT_CERT_REVOKED = "REJECT_CERT_REVOKED"  # 证书被吊销
    REJECT_CERT_EXPIRED = "REJECT_CERT_EXPIRED"  # 证书已过期


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    session_invalidated: bool  # 本次判定是否作废了旧会话

    @property
    def accepted(self) -> bool:
        return self.verdict in (Verdict.ACCEPT_NEW, Verdict.ACCEPT_RESUME)


@dataclass
class _Session:
    binding_id: str
    subject_dn: str
    spki_fingerprint: str
    not_after: float


class SessionBinder:
    """会话-证书绑定校验器。"""

    def __init__(
        self,
        revoked_serials: Optional[Iterable[int]] = None,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self._sessions: Dict[str, _Session] = {}
        self._revoked = set(revoked_serials or ())
        self._now = now or time.time

    # ---- 吊销列表管理 ----
    def revoke(self, serial: int) -> None:
        self._revoked.add(serial)

    def unrevoke(self, serial: int) -> None:
        self._revoked.discard(serial)

    # ---- 内部 ----
    def _invalidate(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    # ---- 主入口 ----
    def validate(self, session_id: str, cert: PeerCertificate) -> Decision:
        """对一次（重）握手做绑定校验。所有拒绝均不降级放行。"""
        now = self._now()
        has_session = session_id in self._sessions

        # 1. 过期检查
        if cert.not_after <= now:
            invalidated = self._invalidate(session_id) if has_session else False
            return Decision(
                Verdict.REJECT_CERT_EXPIRED,
                f"证书已过期(not_after={cert.not_after:.0f} <= now={now:.0f})，拒绝"
                + ("，旧会话已作废" if invalidated else ""),
                invalidated,
            )

        # 2. 吊销检查
        if cert.serial in self._revoked:
            invalidated = self._invalidate(session_id) if has_session else False
            return Decision(
                Verdict.REJECT_CERT_REVOKED,
                f"证书序列号 {cert.serial} 在吊销列表中，拒绝"
                + ("，旧会话已作废" if invalidated else ""),
                invalidated,
            )

        # 3. 绑定检查
        bid = binding_id_for(cert)
        if not has_session:
            self._sessions[session_id] = _Session(
                binding_id=bid,
                subject_dn=cert.subject_dn,
                spki_fingerprint=cert.spki_fingerprint,
                not_after=cert.not_after,
            )
            return Decision(
                Verdict.ACCEPT_NEW,
                f"首次连接，建立绑定 binding_id={bid[:16]}…"
                f"(subject={cert.subject_dn!r}, spki={cert.spki_fingerprint[:16]}…)",
                False,
            )

        existing = self._sessions[session_id]
        if hmac.compare_digest(existing.binding_id, bid):
            return Decision(
                Verdict.ACCEPT_RESUME,
                f"重连绑定一致(binding_id={bid[:16]}…)，恢复会话",
                False,
            )

        self._invalidate(session_id)
        return Decision(
            Verdict.REJECT_CERT_CHANGED,
            "证书更换导致绑定不匹配："
            f"旧(subject={existing.subject_dn!r}, spki={existing.spki_fingerprint[:16]}…)"
            f" != 新(subject={cert.subject_dn!r}, spki={cert.spki_fingerprint[:16]}…)，"
            "拒绝并作废旧会话（需重新完整认证，不降级）",
            True,
        )

    # ---- 观测 ----
    def session_count(self) -> int:
        return len(self._sessions)

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    def session_binding(self, session_id: str) -> Optional[Tuple[str, str, str]]:
        s = self._sessions.get(session_id)
        if s is None:
            return None
        return s.binding_id, s.subject_dn, s.spki_fingerprint
