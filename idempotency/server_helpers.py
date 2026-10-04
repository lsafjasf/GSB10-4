"""Thin HTTP integration layer on top of IdempotencyStore.

Maps library exceptions to deterministic error responses and decorates
replayed responses.  The response BODY never changes between a first
request and a replay; only the ``Idempotency-Replayed`` header is added.
"""

from __future__ import annotations

import json

from idempotency import (
    IdempotencyError,
    REPLAYED_HEADER,
    Response,
)

__all__ = ["error_response", "decorate_replay"]


def error_response(exc: IdempotencyError) -> Response:
    payload = {
        "error": {
            "code": exc.error_code,
            "message": str(exc),
        }
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return Response(
        status=exc.status_code,
        body=body,
        headers=(("Content-Type", "application/json"),),
    )


def decorate_replay(resp: Response, replayed: bool) -> Response:
    if not replayed:
        return resp
    headers = tuple(resp.headers) + ((REPLAYED_HEADER, "true"),)
    return Response(status=resp.status, body=resp.body, headers=headers)
