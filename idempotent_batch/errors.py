"""错误分类。

分类依据只有一条：**这个错误返回时，服务端是否可能已经产生了副作用。**

- RETRYABLE（可重试，且一定没副作用）：请求在服务端执行业务逻辑之前就失败了，
  直接原样重试安全。
- NON_RETRYABLE（不可重试的确定性失败）：服务端执行了业务逻辑并给出确定性结论，
  重试结果不会改变（幂等回放除外）。
- AMBIGUOUS（结果未知 / 疑似已提交）：请求可能已到达服务端、也可能已经提交成功，
  盲目重放可能造成重复副作用。**禁止直接重放**，必须先用幂等键对账：
  查到已提交 => 记成功；确认未提交 => 才能带同一幂等键重新提交。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorKind(str, Enum):
    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class BatchError(Exception):
    """服务端/传输层返回的错误。code 为 HTTP 风格状态码或系统错误码。"""

    code: int
    message: str = ""

    def __str__(self) -> str:  # pragma: no cover - 仅展示用
        return f"<{self.code}> {self.message}"

    @property
    def kind(self) -> ErrorKind:
        return classify(self.code)


# 系统错误码使用负数，避免和 HTTP 状态码冲突。
RETRYABLE_CODES = {
    429: "限流（Too Many Requests），服务端在受理前拒绝，无副作用",
    502: "网关错误（Bad Gateway），上游未执行，无副作用",
    503: "服务暂时不可用（Unavailable），受理前失败，无副作用",
    -1: "连接被拒绝（ECONNREFUSED），请求未发出或未被受理",
    -2: "连接被重置（ECONNRESET），发生在发送请求阶段，服务端未受理",
}

NON_RETRYABLE_CODES = {
    400: "请求格式错误（Bad Request），原样重试永远失败",
    401: "未认证（Unauthorized），凭据问题，重试结果不变",
    403: "被拒绝（Forbidden），确定性的权限/校验失败",
    404: "资源不存在（Not Found），确定性失败",
    422: "语义校验失败（Unprocessable Entity），业务确定性拒绝",
}

AMBIGUOUS_CODES = {
    500: "服务端内部错误：可能在提交前，也可能在提交后，副作用未知",
    504: "网关超时（Gateway Timeout）：上游可能已处理完成，只是响应丢失",
    -3: "读取响应超时（ETIMEDOUT）：请求可能已到达并执行，结果未知",
    -4: "连接在等待响应时中断：服务端可能已经提交",
}

CATALOG = {
    **{c: (ErrorKind.RETRYABLE, why) for c, why in RETRYABLE_CODES.items()},
    **{c: (ErrorKind.NON_RETRYABLE, why) for c, why in NON_RETRYABLE_CODES.items()},
    **{c: (ErrorKind.AMBIGUOUS, why) for c, why in AMBIGUOUS_CODES.items()},
}


def classify(code: int) -> ErrorKind:
    """按错误码分类；未知错误一律按最保守的 AMBIGUOUS 处理。"""
    if code in RETRYABLE_CODES:
        return ErrorKind.RETRYABLE
    if code in NON_RETRYABLE_CODES:
        return ErrorKind.NON_RETRYABLE
    return ErrorKind.AMBIGUOUS


def catalog_rows():
    """供文档/自测打印的分类表：(码, 名称, 分类, 依据与样例)。"""
    return [(code, _name(code), CATALOG[code][0].value, CATALOG[code][1])
            for code in sorted(CATALOG)]


def _name(code: int) -> str:
    return {429: "RATE_LIMITED", 502: "BAD_GATEWAY", 503: "UNAVAILABLE",
            -1: "CONN_REFUSED", -2: "CONN_RESET",
            400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN",
            404: "NOT_FOUND", 422: "UNPROCESSABLE",
            500: "INTERNAL", 504: "GATEWAY_TIMEOUT",
            -3: "READ_TIMEOUT", -4: "RESPONSE_LOST"}.get(code, "UNKNOWN")
