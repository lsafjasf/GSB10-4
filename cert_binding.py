"""会话-证书绑定校验库（仅标准库）。

绑定标识（三者同时绑定，缺一不可）：
  - subject_fp : 证书主体 Subject Name 的 DER 编码的 SHA-256
  - spki_fp    : 证书公钥 SubjectPublicKeyInfo 的 DER 编码的 SHA-256
  - cert_fp    : 整张证书 DER 编码的 SHA-256（含签名，任何字段变动都会改变）

处理规则（确定性，按优先级）：
  1. 证书过期   -> REJECT_EXPIRED，旧会话作废
  2. 证书被吊销 -> REJECT_REVOKED，旧会话作废
  3. 会话未知   -> 绑定新会话（BOUND）
  4. 三项指纹全部一致 -> OK
  5. 任一不一致 -> REJECT_CHANGED，旧会话作废（绝不降级放行）
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Iterable, Optional, Set, Tuple

__all__ = [
    "BindingIdentity",
    "SessionBindingStore",
    "VerifyResult",
    "VerifyStatus",
    "parse_certificate",
    "pem_to_der",
]


class CertFormatError(ValueError):
    """证书 DER/PEM 结构无法解析。"""


# ---------------------------------------------------------------------------
# 最小 DER 解析器（只读，标准库实现）
# ---------------------------------------------------------------------------

def _read_tlv(buf: bytes, off: int) -> Tuple[int, int, int, int]:
    """读取一个 TLV，返回 (tag, header_offset, content_offset, end_offset)。"""
    if off >= len(buf):
        raise CertFormatError("DER 截断：缺少 tag")
    tag = buf[off]
    i = off + 1
    if i >= len(buf):
        raise CertFormatError("DER 截断：缺少 length")
    first = buf[i]
    i += 1
    if first < 0x80:
        length = first
    else:
        n = first & 0x7F
        if n == 0 or n > 8 or i + n > len(buf):
            raise CertFormatError("非法的 DER 长格式长度")
        length = int.from_bytes(buf[i:i + n], "big")
        i += n
    end = i + length
    if end > len(buf):
        raise CertFormatError("DER 截断：内容越界")
    return tag, off, i, end


def _children(buf: bytes, start: int, end: int):
    off = start
    while off < end:
        tag, hdr, content, nxt = _read_tlv(buf, off)
        yield tag, hdr, content, nxt
        off = nxt
    if off != end:
        raise CertFormatError("DER 子元素未对齐")


def _parse_der_time(buf: bytes, content: int, end: int, tag: int) -> datetime:
    raw = buf[content:end].decode("ascii")
    if raw.endswith("Z"):
        suffix = "+00:00"
        raw = raw[:-1]
    elif "+" in raw[8:] or "-" in raw[8:]:
        for idx in range(len(raw) - 1, 8, -1):
            if raw[idx] in "+-":
                suffix = raw[idx:idx + 3] + ":" + raw[idx + 3:idx + 5]
                raw = raw[:idx]
                break
        else:  # pragma: no cover - 防御性分支
            raise CertFormatError("无法解析的时间偏移")
    else:
        raise CertFormatError("证书时间缺少时区标记")
    if tag == 0x17:  # UTCTime: YYMMDDHHMMSS
        fmt = "%y%m%d%H%M%S"
    elif tag == 0x18:  # GeneralizedTime: YYYYMMDDHHMMSS
        fmt = "%Y%m%d%H%M%S"
    else:
        raise CertFormatError("validity 中出现非时间类型 tag=0x%02x" % tag)
    dt = datetime.strptime(raw, fmt)
    return datetime.fromisoformat(dt.isoformat() + suffix).astimezone(timezone.utc)


def pem_to_der(pem: str) -> bytes:
    """PEM 文本 -> DER 字节。"""
    import base64

    lines = [ln.strip() for ln in pem.strip().splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("-----BEGIN "):
        raise CertFormatError("不是合法的 PEM 证书")
    if "CERTIFICATE" not in lines[0]:
        raise CertFormatError("PEM 不是 CERTIFICATE 类型: %s" % lines[0])
    body = "".join(ln for ln in lines[1:] if not ln.startswith("-----END "))
    try:
        return base64.b64decode(body, validate=True)
    except Exception as exc:
        raise CertFormatError("PEM base64 解码失败") from exc


@dataclass(frozen=True, slots=True)
class BindingIdentity:
    """一张证书的绑定标识。"""

    subject_fp: str   # Subject Name DER 的 SHA-256（hex）
    spki_fp: str      # SubjectPublicKeyInfo DER 的 SHA-256（hex）
    cert_fp: str      # 整证书 DER 的 SHA-256（hex）
    serial: int       # 证书序列号
    not_before: datetime
    not_after: datetime

    @classmethod
    def from_pem(cls, pem: str) -> "BindingIdentity":
        return parse_certificate(pem_to_der(pem))

    @classmethod
    def from_der(cls, der: bytes) -> "BindingIdentity":
        return parse_certificate(der)


def parse_certificate(der: bytes) -> BindingIdentity:
    """从 X.509 DER 提取绑定标识（subject/SPKI/整证书指纹 + 序列号 + 有效期）。"""
    tag, hdr, content, end = _read_tlv(der, 0)
    if tag != 0x30 or end != len(der):
        raise CertFormatError("顶层不是完整 SEQUENCE")
    top = list(_children(der, content, end))
    if len(top) != 3:
        raise CertFormatError("Certificate 应恰含 3 个元素")
    tbs = top[0]
    if tbs[0] != 0x30:
        raise CertFormatError("tbsCertificate 不是 SEQUENCE")
    fields = list(_children(der, tbs[2], tbs[3]))
    idx = 0
    if fields and fields[0][0] == 0xA0:  # [0] EXPLICIT version
        idx = 1
    if len(fields) - idx < 6:
        raise CertFormatError("tbsCertificate 字段不足")

    serial_tag, _, serial_c, serial_e = fields[idx]
    if serial_tag != 0x02:
        raise CertFormatError("serialNumber 不是 INTEGER")
    serial = int.from_bytes(der[serial_c:serial_e], "big")

    # 字段顺序: serial, signatureAlg, issuer, validity, subject, SPKI
    issuer = fields[idx + 2]
    validity = fields[idx + 3]
    subject = fields[idx + 4]
    spki = fields[idx + 5]
    if issuer[0] != 0x30 or subject[0] != 0x30 or spki[0] != 0x30:
        raise CertFormatError("issuer/subject/SPKI 结构异常")
    if validity[0] != 0x30:
        raise CertFormatError("validity 不是 SEQUENCE")
    vlist = list(_children(der, validity[2], validity[3]))
    if len(vlist) != 2:
        raise CertFormatError("validity 应含 notBefore/notAfter 两项")
    not_before = _parse_der_time(der, vlist[0][2], vlist[0][3], vlist[0][0])
    not_after = _parse_der_time(der, vlist[1][2], vlist[1][3], vlist[1][0])

    sha = hashlib.sha256
    return BindingIdentity(
        subject_fp=sha(der[subject[1]:subject[3]]).hexdigest(),
        spki_fp=sha(der[spki[1]:spki[3]]).hexdigest(),
        cert_fp=sha(der).hexdigest(),
        serial=serial,
        not_before=not_before,
        not_after=not_after,
    )


# ---------------------------------------------------------------------------
# 会话绑定存储与校验
# ---------------------------------------------------------------------------

class VerifyStatus(Enum):
    OK = "OK"                        # 重连证书与绑定一致
    BOUND = "BOUND"                  # 首次连接，已建立绑定
    REJECT_EXPIRED = "REJECT_EXPIRED"
    REJECT_REVOKED = "REJECT_REVOKED"
    REJECT_CHANGED = "REJECT_CHANGED"


@dataclass(frozen=True, slots=True)
class VerifyResult:
    status: VerifyStatus
    reason: str
    session_alive: bool  # 校验结束后会话是否仍有效

    @property
    def ok(self) -> bool:
        return self.status in (VerifyStatus.OK, VerifyStatus.BOUND)


class _SessionRecord:
    __slots__ = ("subject_fp", "spki_fp", "cert_fp", "serial", "not_after")

    def __init__(self, identity: BindingIdentity) -> None:
        self.subject_fp = identity.subject_fp
        self.spki_fp = identity.spki_fp
        self.cert_fp = identity.cert_fp
        self.serial = identity.serial
        self.not_after = identity.not_after


class SessionBindingStore:
    """会话 -> 证书绑定标识 的存储与校验。

    线程安全。所有拒绝路径都会作废旧会话，绝不降级为单向认证放行。
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, _SessionRecord] = {}
        self._revoked_serials: Set[int] = set()
        self._revoked_cert_fps: Set[str] = set()
        self._lock = threading.RLock()

    # -- 吊销列表 ---------------------------------------------------------

    def revoke(self, *, serial: Optional[int] = None, cert_fp: Optional[str] = None) -> None:
        """登记吊销（按序列号或整证书指纹）。"""
        with self._lock:
            if serial is not None:
                self._revoked_serials.add(serial)
            if cert_fp is not None:
                self._revoked_cert_fps.add(cert_fp)

    def revoke_many(self, serials: Iterable[int] = (), cert_fps: Iterable[str] = ()) -> None:
        with self._lock:
            self._revoked_serials.update(serials)
            self._revoked_cert_fps.update(cert_fps)

    # -- 核心校验 ----------------------------------------------------------

    def verify(
        self,
        session_id: str,
        identity: BindingIdentity,
        *,
        now: Optional[datetime] = None,
    ) -> VerifyResult:
        """校验一次（重）连接。

        now 可注入以便确定性测试；缺省取当前 UTC 时间。
        """
        if now is None:
            now = datetime.now(timezone.utc)
        with self._lock:
            record = self._sessions.get(session_id)

            # 1) 过期：任何情况下都拒绝
            if now > identity.not_after:
                self._sessions.pop(session_id, None)
                return VerifyResult(
                    VerifyStatus.REJECT_EXPIRED,
                    "证书已过期(notAfter=%s)，拒绝并作废旧会话"
                    % identity.not_after.isoformat(),
                    session_alive=False,
                )

            # 2) 吊销：按序列号或整证书指纹命中即拒绝
            if (
                identity.serial in self._revoked_serials
                or identity.cert_fp in self._revoked_cert_fps
            ):
                self._sessions.pop(session_id, None)
                return VerifyResult(
                    VerifyStatus.REJECT_REVOKED,
                    "证书已被吊销(serial=%d)，拒绝并作废旧会话" % identity.serial,
                    session_alive=False,
                )

            # 3) 首次连接：建立绑定
            if record is None:
                self._sessions[session_id] = _SessionRecord(identity)
                return VerifyResult(
                    VerifyStatus.BOUND,
                    "首次连接，已将会话绑定到证书指纹 %s" % identity.cert_fp[:16],
                    session_alive=True,
                )

            # 4) 三项指纹全部一致：正常重连
            if (
                identity.subject_fp == record.subject_fp
                and identity.spki_fp == record.spki_fp
                and identity.cert_fp == record.cert_fp
            ):
                return VerifyResult(
                    VerifyStatus.OK,
                    "证书与绑定一致（主体/公钥/整证书指纹均匹配）",
                    session_alive=True,
                )

            # 5) 任一不一致：拒绝并作废旧会话，说明具体差异
            diffs = []
            if identity.subject_fp != record.subject_fp:
                diffs.append("主体指纹")
            if identity.spki_fp != record.spki_fp:
                diffs.append("公钥指纹")
            if identity.cert_fp != record.cert_fp:
                diffs.append("证书指纹")
            self._sessions.pop(session_id, None)
            return VerifyResult(
                VerifyStatus.REJECT_CHANGED,
                "证书与绑定不一致（%s 不匹配），拒绝并作废旧会话" % "、".join(diffs),
                session_alive=False,
            )

    # -- 管理接口 ----------------------------------------------------------

    def unbind(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def is_bound(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
