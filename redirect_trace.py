"""Redirect chain tracer with credential-safe header policy.

Standard library only. Transport-agnostic: the tracer drives a user-supplied
``fetcher`` callable, so it can run against a real HTTP client or a scripted
fake for deterministic tests/benchmarks.

Fetcher protocol:
    fetcher(url: str, request_headers: dict[str, str]) -> tuple[int, Mapping]
    Returns (status_code, response_headers). The tracer never follows
    redirects inside the fetcher; it walks the chain itself hop by hop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

MAX_HOPS_DEFAULT = 20

# Headers that may carry credentials and must not leak across origins.
SENSITIVE_HEADERS = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
})

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_PCT_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_CTL_RE = re.compile(r"[\x00-\x20\x7f]")

DEFAULT_PORT = {"http": 80, "https": 443}


class InvalidTarget(Exception):
    """Raised when a redirect target cannot be parsed or is not allowed."""


def _decode_unreserved(text: str) -> str:
    """Percent-decode only unreserved characters (RFC 3986 section 6.2.2.2)."""

    def repl(match: re.Match) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else "%" + match.group(1).upper()

    return _PCT_RE.sub(repl, text)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4 dot-segment removal."""
    output = ""
    buf = path
    while buf:
        if buf.startswith("../"):
            buf = buf[3:]
        elif buf.startswith("./"):
            buf = buf[2:]
        elif buf.startswith("/./"):
            buf = "/" + buf[3:]
        elif buf == "/.":
            buf = "/"
        elif buf.startswith("/../"):
            buf = "/" + buf[4:]
            output = output.rsplit("/", 1)[0]
        elif buf == "/..":
            buf = "/"
            output = output.rsplit("/", 1)[0]
        elif buf in (".", ".."):
            buf = ""
        else:
            if buf.startswith("/"):
                idx = buf.find("/", 1)
            else:
                idx = buf.find("/")
            if idx == -1:
                output += buf
                buf = ""
            else:
                output += buf[:idx]
                buf = buf[idx:]
    return output


def normalize_url(url: str, base: Optional[str] = None) -> "NormalizedURL":
    """Resolve ``url`` (optionally against ``base``) and normalize it.

    Raises InvalidTarget for empty input, control characters, non-http(s)
    schemes, missing host, or bad ports.
    """
    if base is not None:
        url = urljoin(base, url)
    if not isinstance(url, str) or not url.strip():
        raise InvalidTarget("empty URL")
    if _CTL_RE.search(url):
        raise InvalidTarget("URL contains whitespace/control characters")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise InvalidTarget(f"unparseable URL: {exc}")

    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORT:
        raise InvalidTarget(f"unsupported scheme: {parts.scheme!r}")

    host = parts.hostname
    if not host:
        raise InvalidTarget("missing host")
    host = host.lower()

    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidTarget(f"invalid port: {exc}")
    effective_port = port if port is not None else DEFAULT_PORT[scheme]

    has_credentials = parts.username is not None or parts.password is not None

    path = _decode_unreserved(parts.path)
    path = _remove_dot_segments(path)
    if not path:
        path = "/"
    query = _decode_unreserved(parts.query)

    # Normalized netloc drops userinfo and the default port.
    netloc = host
    if ":" in host and not host.startswith("["):  # IPv6 literal
        netloc = f"[{host}]"
    if port is not None and port != DEFAULT_PORT[scheme]:
        netloc = f"{netloc}:{port}"

    normalized = urlunsplit((scheme, netloc, path, query, ""))
    return NormalizedURL(
        url=normalized,
        scheme=scheme,
        host=host,
        port=effective_port,
        has_credentials=has_credentials,
    )


@dataclass(frozen=True)
class NormalizedURL:
    url: str
    scheme: str
    host: str
    port: int
    has_credentials: bool

    @property
    def origin(self) -> tuple:
        return (self.scheme, self.host, self.port)


def apply_header_policy(prev: Optional[NormalizedURL],
                        nxt: NormalizedURL,
                        headers: Mapping[str, str]) -> tuple:
    """Decide which request headers may be sent to ``nxt``.

    Policy: sensitive headers (Authorization, Proxy-Authorization, Cookie)
    are only forwarded within the same origin (scheme, host, effective port).
    Any cross-scheme or cross-host (or cross-port) hop strips them. This
    covers the HTTPS -> HTTP downgrade case, which is a scheme change.

    Returns (headers_to_send, stripped_header_names).
    """
    if prev is None or prev.origin == nxt.origin:
        return dict(headers), []
    kept = {k: v for k, v in headers.items() if k.lower() not in SENSITIVE_HEADERS}
    stripped = sorted(
        (k for k in headers if k.lower() in SENSITIVE_HEADERS),
        key=str.lower,
    )
    return kept, stripped


@dataclass
class Hop:
    seq: int
    url: str
    scheme: str
    host: str
    port: int
    url_has_credentials: bool
    sensitive_headers_sent: list = field(default_factory=list)
    sensitive_headers_stripped: list = field(default_factory=list)
    status: Optional[int] = None
    location: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "url": self.url,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "url_has_credentials": self.url_has_credentials,
            "sensitive_headers_sent": self.sensitive_headers_sent,
            "sensitive_headers_stripped": self.sensitive_headers_stripped,
            "status": self.status,
            "location": self.location,
        }


@dataclass
class TraceResult:
    status: str  # "ok" | "loop" | "too_many_redirects" | "invalid_url"
    reason: str
    hops: list = field(default_factory=list)
    final_url: Optional[str] = None

    @property
    def depth(self) -> int:
        return len(self.hops)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "depth": self.depth,
            "final_url": self.final_url,
            "hops": [h.to_dict() for h in self.hops],
        }


class RedirectTracer:
    """Walks redirect chains with loop/length/validity guards."""

    def __init__(self, fetcher: Callable, max_hops: int = MAX_HOPS_DEFAULT):
        if max_hops < 1:
            raise ValueError("max_hops must be >= 1")
        self.fetcher = fetcher
        self.max_hops = max_hops

    def trace(self, url: str,
              headers: Optional[Mapping[str, str]] = None) -> TraceResult:
        pending_headers = dict(headers or {})
        try:
            current = normalize_url(url)
        except InvalidTarget as exc:
            return TraceResult("invalid_url", f"start URL rejected: {exc}")

        seen = set()
        hops: list = []
        prev: Optional[NormalizedURL] = None

        while True:
            if current.url in seen:
                return TraceResult(
                    "loop",
                    f"redirect loop detected at {current.url}",
                    hops, current.url)
            if len(hops) >= self.max_hops:
                return TraceResult(
                    "too_many_redirects",
                    f"chain exceeded max_hops={self.max_hops}",
                    hops, current.url)
            seen.add(current.url)

            send_headers, stripped = apply_header_policy(
                prev, current, pending_headers)
            hop = Hop(
                seq=len(hops),
                url=current.url,
                scheme=current.scheme,
                host=current.host,
                port=current.port,
                url_has_credentials=current.has_credentials,
                sensitive_headers_sent=sorted(
                    (k for k in send_headers if k.lower() in SENSITIVE_HEADERS),
                    key=str.lower),
                sensitive_headers_stripped=stripped,
            )
            hops.append(hop)

            status, resp_headers = self.fetcher(current.url, send_headers)
            hop.status = status
            location = None
            for key, value in resp_headers.items():
                if key.lower() == "location":
                    location = value
                    break
            hop.location = location

            if status not in REDIRECT_STATUSES:
                return TraceResult("ok", f"terminal status {status}",
                                   hops, current.url)
            if not location:
                return TraceResult(
                    "invalid_url",
                    f"status {status} without Location header",
                    hops, current.url)

            try:
                nxt = normalize_url(location, base=current.url)
            except InvalidTarget as exc:
                return TraceResult(
                    "invalid_url",
                    f"bad redirect target {location!r}: {exc}",
                    hops, current.url)

            # Only sensitive headers that survived the previous hop are
            # candidates for the next one; non-sensitive headers pass through.
            pending_headers = send_headers
            prev, current = current, nxt


class ScriptedFetcher:
    """Deterministic fake transport for tests and benchmarks.

    script: {normalized_url: (status, location_or_None)}
    Unknown URLs answer 200. Records every request for verification.
    """

    def __init__(self, script: Mapping[str, tuple]):
        self.script = dict(script)
        self.requests = []

    def __call__(self, url: str, request_headers: Mapping[str, str]):
        self.requests.append((url, dict(request_headers)))
        status, location = self.script.get(url, (200, None))
        headers = {"Location": location} if location is not None else {}
        return status, headers


class HttpFetcher:
    """Real single-request HTTP(S) fetcher (stdlib http.client).

    Performs exactly one request and returns (status, headers); it never
    follows redirects itself.
    """

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def __call__(self, url: str, request_headers: Mapping[str, str]):
        import http.client

        parts = urlsplit(url)
        conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                    else http.client.HTTPConnection)
        port = parts.port or DEFAULT_PORT[parts.scheme]
        conn = conn_cls(parts.hostname, port, timeout=self.timeout)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            conn.request("GET", path, headers=dict(request_headers))
            resp = conn.getresponse()
            resp.read()
            return resp.status, dict(resp.getheaders())
        finally:
            conn.close()
