"""Minimal HTTP integration demo (standard library only).

Run:  python3 examples/http_demo.py
Then: curl -i -X POST localhost:8000/orders -H 'Idempotency-Key: ord_demo_00001' -d '{"sku":"A"}'
      (repeat the same curl -> identical body + 'Idempotency-Replayed: true')
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from idempotency import (
    IdempotencyError,
    IdempotencyStore,
    Response,
    request_fingerprint,
)
from server_helpers import decorate_replay, error_response

STORE = IdempotencyStore()
ORDERS = {}


def create_order(body: bytes) -> Response:
    payload = json.loads(body or b"{}")
    order_id = f"O-{len(ORDERS) + 1}"
    ORDERS[order_id] = payload
    out = json.dumps({"order_id": order_id, "status": "CREATED"}).encode()
    return Response(201, out, (("Content-Type", "application/json"),))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        key = self.headers.get("Idempotency-Key", "")
        user = self.headers.get("X-User-Id", "anonymous")
        endpoint = f"POST {self.path}"
        try:
            resp, replayed = STORE.execute(
                user, endpoint, key,
                request_fingerprint("POST", self.path, body),
                lambda: create_order(body),
            )
            resp = decorate_replay(resp, replayed)
        except IdempotencyError as exc:
            resp = error_response(exc)
        self.send_response(resp.status)
        for k, v in resp.headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(resp.body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("listening on :8000")
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
