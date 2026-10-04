"""重定向链追踪与长度限制库（仅标准库）。

核心能力：
- 完整记录每一跳：协议、主机、端口、状态码、发送的头部、是否携带凭据。
- 跨协议 / 跨主机（源变化）时按策略剥离敏感头部。
- 终止分类：OK / LOOP / CHAIN_TOO_LONG / INVALID_TARGET / FETCH_ERROR。
- 相对地址、转义路径的解析与规范化。

fetch 回调签名：fetch(url: str, headers: dict) -> (status: int, headers: dict)
由调用方注入（自测用内存桩，真实环境可用 http.client 实现）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from urllib.parse import urlsplit, urljoin, unquote, quote
import re

# 视为敏感凭据的请求头（小写匹配，HTTP 头名大小写不敏感）
DEFAULT_SENSITIVE_HEADERS = (
    "authorization",
    "proxy-authorization",
    "cookie",
)

REDIRECT_STATUSES = {301, 302, 303, 307, 308}

DEFAULT_MAX_REDIRECTS = 20

# 终止原因
REASON_OK = "OK"                       # 到达非重定向响应
REASON_LOOP = "LOOP"                   # 规范化 URL 重复出现
REASON_CHAIN_TOO_LONG = "CHAIN_TOO_LONG"  # 跳数超过上限
REASON_INVALID_TARGET = "INVALID_TARGET"  # 非法 Location / 目标地址
REASON_FETCH_ERROR = "FETCH_ERROR"     # 请求回调抛错

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}

# RFC 3986 无保留字符：规范化时这些字符应保持不解义形式之外的原样
_UNRESERVED = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
# WHATWG URL 解析时需要剥离的首尾控制字符 / 空白
_STRIP_CHARS = "\t\n\r\u000c "


class InvalidTargetError(ValueError):
    """Location 头无法解析为合法的 http/https 目标。"""


def _normalize_percent_encoding(path: str) -> str:
    """规范化百分号编码：无保留字符解义，其余转义统一大写十六进制。"""
    out = []
    i = 0
    n = len(path)
    while i < n:
        ch = path[i]
        if ch == "%" and i + 2 < n and re.match(r"[0-9A-Fa-f]{2}", path[i + 1:i + 3]):
            pct = path[i:i + 3]
            decoded = unquote(pct)
            if decoded in _UNRESERVED:
                out.append(decoded)          # %7E -> ~
            else:
                out.append(("%" + pct[1:].upper()))  # %2f -> %2F
            i += 3
        else:
            if ch in _UNRESERVED:
                out.append(ch)
            elif ch == "%":
                out.append("%25")            # 孤立的 % 非法，转义掉
            else:
                # 保留原样（含 / 与已解义字节）；非 ASCII 统一按 UTF-8 重新转义
                if ord(ch) > 127:
                    out.append(quote(ch, safe="/"))
                else:
                    out.append(ch)
            i += 1
    return "".join(out)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 5.2.4 remove_dot_segments。"""
    out = []
    for seg in path.split("/"):
        if seg == ".":
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    result = "/".join(out)
    if path.endswith(("/", "/.", "/..")) and not result.endswith("/"):
        result += "/"
    return result


def normalize_url(url: str) -> str:
    """把 URL 规范化为用于比对的稳定形态。

    - scheme/host 小写；默认端口去除；fragment 丢弃
    - 路径做 dot-segment 消除与百分号编码规范化
    - 空路径补 "/"
    非法地址抛 InvalidTargetError。
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidTargetError("空 URL")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidTargetError(f"不支持的协议: {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise InvalidTargetError("缺少主机名")
    host = host.lower().rstrip(".")
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidTargetError(f"非法端口: {exc}") from exc
    if port == DEFAULT_PORTS[scheme]:
        port = None
    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += ":" + parts.password
        userinfo += "@"
    path = parts.path or "/"
    path = _remove_dot_segments(path)
    path = _normalize_percent_encoding(path)
    query = parts.query
    netloc = userinfo + host + (f":{port}" if port else "")
    result = f"{scheme}://{netloc}{path}"
    if query:
        result += "?" + query
    return result


def split_origin(url: str) -> tuple[str, str, int | None]:
    """返回 (scheme, host, port)，port 为默认端口时归一为 None。"""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port
    if port == DEFAULT_PORTS.get(scheme):
        port = None
    return scheme, parts.hostname.lower(), port


def resolve_url(base: str, location: str) -> dict:
    """解析 Location（允许相对地址），返回规范化结果及解析细节。

    返回: {raw, joined, normalized, scheme, host, port, has_userinfo}
    非法时抛 InvalidTargetError。
    """
    if location is None:
        raise InvalidTargetError("缺少 Location")
    cleaned = location.strip(_STRIP_CHARS)
    if not cleaned:
        raise InvalidTargetError("空 Location")
    # 控制字符内嵌（非首尾空白）一律视为非法
    if any(ord(c) < 0x20 for c in cleaned):
        raise InvalidTargetError("Location 含控制字符")
    joined = urljoin(base, cleaned)
    normalized = normalize_url(joined)
    parts = urlsplit(joined)
    return {
        "raw_location": location,
        "joined": joined,
        "normalized": normalized,
        "scheme": parts.scheme.lower(),
        "host": parts.hostname.lower(),
        "port": parts.port,
        "has_userinfo": parts.username is not None,
    }


def is_same_origin(url_a: str, url_b: str) -> bool:
    return split_origin(url_a) == split_origin(url_b)


@dataclass
class Hop:
    index: int
    url: str                       # 规范化后的请求 URL
    scheme: str
    host: str
    port: int | None
    status: int | None             # 响应状态码（末跳无重定向时为真实状态）
    request_headers: dict          # 实际发送的头部（敏感值以 "<redacted>" 记录）
    stripped_headers: list         # 本跳被策略剥离的头名
    credentials_carried: bool      # 是否携带敏感头或 URL 内嵌凭据
    url_has_userinfo: bool
    location_raw: str | None = None
    resolved_next: str | None = None
    note: str | None = None


@dataclass
class TraceResult:
    start_url: str
    reason: str
    hops: list[Hop] = field(default_factory=list)
    error: str | None = None

    @property
    def depth(self) -> int:
        """重定向次数（跳数 - 1）。"""
        return max(0, len(self.hops) - 1)

    def to_dict(self) -> dict:
        return {
            "start_url": self.start_url,
            "reason": self.reason,
            "depth": self.depth,
            "error": self.error,
            "hops": [asdict(h) for h in self.hops],
        }


class RedirectTracer:
    def __init__(
        self,
        fetch,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        sensitive_headers=DEFAULT_SENSITIVE_HEADERS,
    ):
        self.fetch = fetch
        self.max_redirects = max_redirects
        self.sensitive = {h.lower() for h in sensitive_headers}

    def _apply_policy(self, headers: dict, from_url: str, to_url: str):
        """跨源（跨协议或跨主机/端口）时剥离敏感头。"""
        kept, stripped = {}, []
        if is_same_origin(from_url, to_url):
            return headers, stripped, "same-origin"
        for name, value in headers.items():
            if name.lower() in self.sensitive:
                stripped.append(name)
            else:
                kept[name] = value
        return kept, stripped, "cross-origin"

    def trace(self, start_url: str, headers: dict | None = None) -> TraceResult:
        headers = dict(headers or {})
        try:
            current = normalize_url(start_url)
        except InvalidTargetError as exc:
            return TraceResult(start_url=start_url, reason=REASON_INVALID_TARGET,
                               error=str(exc))

        seen: set[str] = set()
        hops: list[Hop] = []
        url = current

        while True:
            if url in seen:
                hops[-1].note = (hops[-1].note or "") + " 下一跳指向已访问地址"
                return TraceResult(start_url, REASON_LOOP, hops,
                                   error=f"重定向成环: {url}")
            seen.add(url)

            scheme, host, port = split_origin(url)
            url_parts = urlsplit(url)
            url_has_userinfo = url_parts.username is not None
            redacted = {
                k: ("<redacted>" if k.lower() in self.sensitive else v)
                for k, v in headers.items()
            }
            hop_idx = len(hops)
            hop = Hop(
                index=hop_idx, url=url, scheme=scheme, host=host, port=port,
                status=None, request_headers=redacted, stripped_headers=[],
                credentials_carried=url_has_userinfo
                or any(k.lower() in self.sensitive for k in headers),
                url_has_userinfo=url_has_userinfo,
            )
            hops.append(hop)

            try:
                status, resp_headers = self.fetch(url, headers)
            except Exception as exc:  # 回调自身的错误按 FETCH_ERROR 分类
                hop.status = None
                return TraceResult(start_url, REASON_FETCH_ERROR, hops,
                                   error=f"{type(exc).__name__}: {exc}")
            hop.status = status

            if status not in REDIRECT_STATUSES:
                return TraceResult(start_url, REASON_OK, hops)

            location = None
            for k, v in (resp_headers or {}).items():
                if k.lower() == "location":
                    location = v
                    break
            if not location:
                # 重定向状态码但没有 Location：视为到达终点
                hop.note = "重定向状态码缺少 Location，按终点处理"
                return TraceResult(start_url, REASON_OK, hops)

            try:
                resolved = resolve_url(url, location)
            except InvalidTargetError as exc:
                hop.location_raw = location
                return TraceResult(start_url, REASON_INVALID_TARGET, hops,
                                   error=f"非法重定向目标: {exc}")
            hop.location_raw = location
            hop.resolved_next = resolved["normalized"]

            next_url = resolved["normalized"]
            if next_url in seen:
                # 环：当前 hop 已发出，下一跳不再请求
                hop.note = f"目标 {next_url} 已在链中出现"
                return TraceResult(start_url, REASON_LOOP, hops,
                                   error=f"重定向成环: {next_url}")

            headers, stripped, scope = self._apply_policy(headers, url, next_url)
            hop.stripped_headers = stripped
            if stripped:
                hop.note = (f"{scope}: 剥离 {', '.join(stripped)}"
                            f"（{url} -> {next_url}）")
            elif scope == "cross-origin":
                hop.note = f"{scope}: 无敏感头需要剥离（{url} -> {next_url}）"

            if len(hops) > self.max_redirects:
                return TraceResult(start_url, REASON_CHAIN_TOO_LONG, hops,
                                   error=f"超过最大重定向数 {self.max_redirects}")
            url = next_url
