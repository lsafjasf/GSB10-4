"""HTTP 请求头缓存键规范化（仅标准库）。

设计目标：语义不同的请求绝不落到同一个缓存键上（宁可多占缓存，不可错误命中）。

公开 API：
    canonical(headers, vary) -> bytes   规范化的可注入性安全的字节串
    key(headers, vary)       -> str     缓存键（短键可读，长键哈希，绝不截断）
    normalize_value(name, value) -> str 单个头部的规范化值（供幂等性验证）
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence, Tuple, Union

__all__ = [
    "Policy",
    "DEFAULT_POLICIES",
    "CacheKeyError",
    "InvalidHeaderName",
    "InvalidHeaderValue",
    "HeaderBlockTooLarge",
    "canonical",
    "key",
    "normalize_value",
    "MAX_KEY_LEN",
    "SHORT_KEY_MAX",
    "MAX_VARY_BLOCK",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

VERSION = "v1"                      # 规范化规则版本；规则变更必须升级版本号
HASH_PREFIX = "h1"                  # 哈希键前缀（域分隔，跨档不碰撞）
SHORT_KEY_MAX = 256                 # 规范化字节串 <= 该长度时用可读键
MAX_VARY_BLOCK = 63 * 1024          # 参与键的头部块总字节上限，超出拒绝（不截断）
MAX_KEY_LEN = 3 + 3 * SHORT_KEY_MAX  # 键长硬上界 771：可读键最坏每字节 %XX；
                                    # 哈希键 "h1:<len>:<64hex>" <= 74，恒更小

TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
OWS_RUN_RE = re.compile(r"[ \t]+")
OBS_FOLD_RE = re.compile(r"\r\n[ \t]+")   # obs-fold：CRLF 后接 SP/HTAB，语义等价于 SP

# 值中禁止出现的控制字符（除 HTAB 外）。obs-fold 已先行替换，此处再出现 CR/LF 即非法。
_CTL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

# 可读键允许原样出现的字符（其余一律 %XX 百分号编码；'%%' 本身必编码，保证可逆解析）
_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "!#$&'()*+,-./:;<=>?@[]^_`{|}~"
)

Headers = Union[Mapping[str, str], Iterable[Tuple[str, str]]]


class CacheKeyError(ValueError):
    """缓存键构造失败的基类。"""


class InvalidHeaderName(CacheKeyError):
    """头部名非法（非 token、含空白或控制字符）。"""


class InvalidHeaderValue(CacheKeyError):
    """头部值非法（含未折叠的 CR/LF 或其他控制字符）。"""


class HeaderBlockTooLarge(CacheKeyError):
    """参与键的头部块超过上限；拒绝而非截断。"""


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """单个头部的规范化策略。

    mode:
        "exact"  仅做 RFC 7230 保证安全的变换：去首尾 OWS、obs-fold 展开。
                 值的其余字节（大小写、内部空白、顺序）全部保留 —— 绝不合并。
        "list"   按 list 语法解析（逗号分元素、分号分参数，引号/反斜杠转义感知）。
        "single" 按单元素解析（仅分号参数，逗号视为普通字符）。
    sort_elements:   元素顺序是否无关（True 时排序，重复元素保留不去重）。
    sort_params:     参数顺序是否无关（True 时排序，重复参数保留不去重）。
    casefold_token:  元素主 token 是否按规范大小写不敏感（True 时转小写）。
    casefold_params: 参数名是否大小写不敏感（True 时转小写；参数值永不折叠）。

    注意：参数值、引号串内容、exact 值的内部字节一律原样保留，
    任何情况下都不会把语义可能不同的值合并。
    """

    mode: str = "exact"
    sort_elements: bool = False
    sort_params: bool = False
    casefold_token: bool = False
    casefold_params: bool = False

    def fingerprint(self) -> str:
        return (
            f"{self.mode}/{int(self.sort_elements)}{int(self.sort_params)}"
            f"{int(self.casefold_token)}{int(self.casefold_params)}"
        )


# 默认策略表：仅对规范明确声明“大小写不敏感 / 顺序无关”的语法做归一化。
DEFAULT_POLICIES = {
    # RFC 9110 §12.5.1：media-range 大小写不敏感，参数名不敏感，参数值保留
    "accept": Policy("list", sort_elements=False, sort_params=True,
                     casefold_token=True, casefold_params=True),
    # RFC 9110 §12.5.3：content-coding token 大小写不敏感；顺序表达偏好，保留
    "accept-encoding": Policy("list", sort_elements=False, sort_params=True,
                              casefold_token=True, casefold_params=True),
    # RFC 9110 §12.5.4：language-range 大小写不敏感；顺序表达偏好，保留
    "accept-language": Policy("list", sort_elements=False, sort_params=True,
                              casefold_token=True, casefold_params=True),
    # RFC 9110 §8.3.1：media type 大小写不敏感，参数名不敏感，参数值保留
    "content-type": Policy("single", sort_params=True,
                           casefold_token=True, casefold_params=True),
    # 未在表中的头部一律走 exact（最保守），授权头尤其如此。
}


# ---------------------------------------------------------------------------
# 输入校验与基础规范化
# ---------------------------------------------------------------------------


def _iter_headers(headers: Headers) -> Iterable[Tuple[str, str]]:
    if isinstance(headers, Mapping):
        return headers.items()
    return headers


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not TOKEN_RE.match(name):
        raise InvalidHeaderName(f"非法头部名: {name!r}")
    return name.lower()


def _unfold(value: str) -> str:
    """obs-fold 展开为 SP，并拒绝其余控制字符。"""
    value = OBS_FOLD_RE.sub(" ", value)
    if _CTL_RE.search(value):
        raise InvalidHeaderValue(f"头部值含非法控制字符: {value!r}")
    return value


def _strip_ows(value: str) -> str:
    return value.strip(" \t")


def _collapse_ows_outside_quotes(text: str) -> str:
    """把引号串之外的 OWS 游程折叠为单个 SP；引号串内部逐字节保留。"""
    out = []
    in_quotes = False
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if in_quotes:
            out.append(ch)
            if ch == "\\" and idx + 1 < length:
                out.append(text[idx + 1])
                idx += 2
                continue
            if ch == '"':
                in_quotes = False
            idx += 1
        else:
            if ch == '"':
                in_quotes = True
                out.append(ch)
                idx += 1
            elif ch == " " or ch == "\t":
                idx += 1
                while idx < length and text[idx] in " \t":
                    idx += 1
                out.append(" ")
            else:
                out.append(ch)
                idx += 1
    return "".join(out)


def _casefold_outside_quotes(text: str) -> str:
    """引号串之外转小写；引号串内部逐字节保留。"""
    out = []
    in_quotes = False
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if in_quotes:
            out.append(ch)
            if ch == "\\" and idx + 1 < length:
                out.append(text[idx + 1])
                idx += 2
                continue
            if ch == '"':
                in_quotes = False
            idx += 1
        else:
            if ch == '"':
                in_quotes = True
                out.append(ch)
            else:
                out.append(ch.lower())
            idx += 1
    return "".join(out)


def _split_top(text: str, sep: str) -> list:
    """按顶层分隔符切分（引号串与反斜杠转义感知），保留空段。"""
    parts = []
    buf = []
    in_quotes = False
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if in_quotes:
            buf.append(ch)
            if ch == "\\" and idx + 1 < length:
                buf.append(text[idx + 1])
                idx += 2
                continue
            if ch == '"':
                in_quotes = False
            idx += 1
        else:
            if ch == '"':
                in_quotes = True
                buf.append(ch)
                idx += 1
            elif ch == sep:
                parts.append("".join(buf))
                buf = []
                idx += 1
            else:
                buf.append(ch)
                idx += 1
    parts.append("".join(buf))
    return parts


def _split_first_unquoted_eq(text: str) -> Tuple[str, Union[str, None]]:
    """在第一个引号外的 '=' 处切分；无 '=' 返回 (text, None)。"""
    in_quotes = False
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if in_quotes:
            if ch == "\\" and idx + 1 < length:
                idx += 2
                continue
            if ch == '"':
                in_quotes = False
            idx += 1
        else:
            if ch == '"':
                in_quotes = True
                idx += 1
            elif ch == "=":
                return text[:idx], text[idx + 1:]
            else:
                idx += 1
    return text, None


# ---------------------------------------------------------------------------
# 值规范化
# ---------------------------------------------------------------------------


def _norm_params(segment: str, policy: Policy) -> str:
    """规范化分号参数段（segment 不含元素主体，形如 '; a=1; B="x"'）。"""
    if not segment:
        return ""
    params = []
    for raw in _split_top(segment[1:], ";"):
        raw = _strip_ows(raw)
        if not raw:
            continue
        name, value = _split_first_unquoted_eq(raw)
        name = _strip_ows(name)
        if policy.casefold_params:
            name = name.lower()
        if value is None:
            params.append((name, None))
        else:
            params.append((name, _strip_ows(value)))
    if policy.sort_params:
        params.sort(key=lambda p: (p[0], "" if p[1] is None else p[1]))
    out = []
    for name, value in params:
        out.append(name if value is None else f"{name}={value}")
    return ";" + ";".join(out)


def _norm_element(element: str, policy: Policy) -> str:
    element = _strip_ows(element)
    if not element:
        return ""
    semi = _split_top(element, ";")
    body = _strip_ows(semi[0])
    if policy.casefold_token:
        body = _casefold_outside_quotes(body)
    return body + _norm_params(element[len(semi[0]):], policy)


def _norm_list(value: str, policy: Policy) -> str:
    value = _collapse_ows_outside_quotes(value)
    elements = [_norm_element(e, policy) for e in _split_top(value, ",")]
    # RFC 7230 §7：空元素无语义，接收方必须忽略；丢弃空元素是规范安全的。
    # 非空的重复元素保留（出现次数可能携带语义）。
    elements = [e for e in elements if e]
    if policy.sort_elements:
        elements = sorted(elements)
    return ",".join(elements)


def _norm_single(value: str, policy: Policy) -> str:
    value = _collapse_ows_outside_quotes(value)
    return _norm_element(value, policy)


def normalize_value(name: str, value: str, policies=None) -> str:
    """按策略规范化单个头部值（name 用于选策略，大小写不敏感）。"""
    policies = DEFAULT_POLICIES if policies is None else policies
    policy = policies.get(name.lower(), Policy())
    value = _strip_ows(_unfold(value))
    if policy.mode == "list":
        return _norm_list(value, policy)
    if policy.mode == "single":
        return _norm_single(value, policy)
    return value  # exact：除首尾 OWS / obs-fold 外逐字节保留


# ---------------------------------------------------------------------------
# 规范化序列化（可注入性安全）
# ---------------------------------------------------------------------------


def _lp(data: bytes) -> bytes:
    """长度前缀编码：<len>:<data>，保证结构可解析、整体注入。"""
    return str(len(data)).encode("ascii") + b":" + data


def canonical(headers: Headers, vary: Sequence[str], policies=None) -> bytes:
    """生成规范化字节串。

    结构（一切字段长度前缀，可唯一解析）：
        HK1\n
        <n>:<vary名1>\x00<策略指纹>\x00 ...   （按小写名排序，含策略指纹）
        \n
        每个 vary 头一条记录：
          0\x00            该头缺失
          1\x00<len>:<规范化值>   该头存在（空值与缺失可区分）
    """
    policies = DEFAULT_POLICIES if policies is None else policies
    vary_names = sorted({_check_name(n) for n in vary})
    if not vary_names:
        raise CacheKeyError("vary 不能为空：不参与键的头部集合必须显式声明")

    buckets = {n: [] for n in vary_names}
    total = 0
    for raw_name, raw_value in _iter_headers(headers):
        name = _check_name(raw_name)
        if name not in buckets:
            continue  # 未声明参与键的头部被忽略 —— 这是调用方显式的契约
        # 值允许 str 或 str 序列（序列表示同名重复头行）
        if isinstance(raw_value, str):
            lines = [raw_value]
        elif isinstance(raw_value, (list, tuple)):
            lines = list(raw_value)
        else:
            raise InvalidHeaderValue(f"头部值必须是 str 或 str 序列: {raw_name!r}")
        for line in lines:
            if not isinstance(line, str):
                raise InvalidHeaderValue(f"头部值必须是 str: {raw_name!r}")
            total += len(line.encode("utf-8"))
            if total > MAX_VARY_BLOCK:
                raise HeaderBlockTooLarge(
                    f"参与键的头部块超过 {MAX_VARY_BLOCK} 字节，拒绝构造（不截断）"
                )
            buckets[name].append(line)

    out = [b"HK1\n"]
    for name in vary_names:
        policy = policies.get(name, Policy())
        out.append(_lp(name.encode("ascii") + b"\x00" + policy.fingerprint().encode("ascii")))
    out.append(b"\n")
    for name in vary_names:
        lines = buckets[name]
        if not lines:
            out.append(b"0\x00")
        else:
            # RFC 7230 §3.2.2：重复头行等价于逗号拼接（Set-Cookie 除外，
            # 本库不将其纳入默认策略，调用方应自行承担风险）。
            combined = ",".join(lines)
            norm = normalize_value(name, combined, policies)
            out.append(b"1\x00" + _lp(norm.encode("utf-8")))
    return b"".join(out)


# ---------------------------------------------------------------------------
# 缓存键
# ---------------------------------------------------------------------------


def _percent_encode(data: bytes) -> str:
    out = []
    for byte in data:
        ch = chr(byte)
        if ch in _SAFE:
            out.append(ch)
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def key(headers: Headers, vary: Sequence[str], policies=None) -> str:
    """生成缓存键。

    - 规范化字节串 <= SHORT_KEY_MAX：可读键 "v1:" + 百分号编码（可逆、无碰撞）。
    - 否则：哈希键 "h1:<原长>:<sha256-hex>"，携带长度标签，绝不截断。
    两档前缀不同，跨档不可能碰撞；同档内碰撞下界为 SHA-256 抗碰撞性。
    """
    canon = canonical(headers, vary, policies)
    if len(canon) <= SHORT_KEY_MAX:
        return f"{VERSION}:" + _percent_encode(canon)
    digest = hashlib.sha256(canon).hexdigest()
    return f"{HASH_PREFIX}:{len(canon)}:{digest}"
