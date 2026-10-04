"""Render rate-limit decisions into HTTP-style responses.

Four format combinations are produced from one Decision:

  allowed / kind=second  -> 200, quota headers, scope "per-second"
  allowed / kind=window  -> 200, quota headers, scope "per-window"
  denied  / kind=second  -> 429, Retry-After + jitter hint, scope "per-second"
  denied  / kind=window  -> 429, Retry-After + jitter hint, scope "per-window"

Header/body values all derive from the same Decision fields, so the
rendered response is consistent with the limiter's internal counters by
construction (see selftest.py for the randomized cross-check).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Dict, Optional

from .hints import JitterSuggester, JitterSuggestion
from .limiter import Decision, KIND_SECOND

SCOPE = {KIND_SECOND: "per-second"}
DEFAULT_SCOPE = "per-window"


@dataclass(frozen=True)
class RateLimitResponse:
    status: int
    headers: Dict[str, str]
    body: str
    decision: Decision
    jitter: Optional[JitterSuggestion]

    def body_json(self) -> dict:
        return json.loads(self.body)


def _scope(kind: str) -> str:
    return SCOPE.get(kind, DEFAULT_SCOPE)


class ResponseBuilder:
    def __init__(self, suggester: Optional[JitterSuggester] = None) -> None:
        self.suggester = suggester if suggester is not None else JitterSuggester()

    def build(self, d: Decision) -> RateLimitResponse:
        scope = _scope(d.kind)
        reset_ms = math.ceil(d.reset_at * 1000.0)
        headers: Dict[str, str] = {
            "X-RateLimit-Limit": str(d.limit),
            "X-RateLimit-Remaining": str(d.remaining),
            "X-RateLimit-Reset-Ms": str(reset_ms),
            "X-RateLimit-Window-Ms": str(d.remaining_ms),
        }
        if d.allowed:
            body = {
                "status": "allowed",
                "scope": scope,
                "limit": d.limit,
                "used": d.used,
                "remaining": d.remaining,
                "window_ms": d.remaining_ms,
                "reset_ms": reset_ms,
            }
            return RateLimitResponse(
                status=200,
                headers=headers,
                body=json.dumps(body, sort_keys=True),
                decision=d,
                jitter=None,
            )

        s = self.suggester.suggest(d.remaining_raw)
        headers["Retry-After"] = str(math.ceil(s.hint_ms / 1000.0))
        headers["X-RateLimit-Retry-After-Ms"] = str(s.hint_ms)
        headers["X-RateLimit-Jitter-Ms"] = str(s.jitter_ms)
        body = {
            "status": "denied",
            "scope": scope,
            "error": "rate_limited",
            "limit": d.limit,
            "used": d.used,
            "remaining": d.remaining,
            "window_ms": d.remaining_ms,
            "reset_ms": reset_ms,
            "retry_after_ms": s.hint_ms,
            "retry_after_s": math.ceil(s.hint_ms / 1000.0),
            "base_ms": s.base_ms,
            "jitter_ms": s.jitter_ms,
            "jitter_index": s.jitter_index,
            "jitter_seed": self.suggester.seed,
            "jitter_spread_ms": self.suggester.spread_ms,
            "clamped": s.clamped,
            "clock_skew_count": d.clock_skew_count,
        }
        return RateLimitResponse(
            status=429,
            headers=headers,
            body=json.dumps(body, sort_keys=True),
            decision=d,
            jitter=s,
        )
