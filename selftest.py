"""redirect_tracer 自测（unittest，仅标准库）。

运行: python3 selftest.py -v
"""

import unittest

from redirect_tracer import (
    RedirectTracer,
    normalize_url,
    resolve_url,
    is_same_origin,
    REASON_OK,
    REASON_LOOP,
    REASON_CHAIN_TOO_LONG,
    REASON_INVALID_TARGET,
    REASON_FETCH_ERROR,
    InvalidTargetError,
)

AUTH = {"Authorization": "Bearer s3cret", "Cookie": "sid=abc",
        "X-Request-Id": "r-1"}


def make_fetch(routes):
    """routes: {url: (status, headers)}；未命中返回 200。"""
    def fetch(url, headers):
        return routes.get(url, (200, {}))
    return fetch


def chain(start, *hops):
    """构造线性链: start -> hops[0] -> ... -> 终点(200)。"""
    routes = {}
    seq = [start, *hops]
    for i in range(len(seq) - 1):
        routes[seq[i]] = (302, {"Location": seq[i + 1]})
    routes[seq[-1]] = (200, {})
    return routes


class TestDirectRedirect(unittest.TestCase):
    def test_same_origin_redirect_keeps_headers(self):
        a = "http://example.com/a"
        b = "http://example.com/b"
        tracer = RedirectTracer(make_fetch(chain(a, b)))
        result = tracer.trace(a, headers=AUTH)
        self.assertEqual(result.reason, REASON_OK)
        self.assertEqual(result.depth, 1)
        self.assertEqual(len(result.hops), 2)
        self.assertEqual(result.hops[0].resolved_next, b)
        self.assertEqual(result.hops[0].stripped_headers, [])
        self.assertTrue(result.hops[0].credentials_carried)
        self.assertTrue(result.hops[1].credentials_carried)
        # 记录中敏感值必须被打码
        self.assertEqual(result.hops[0].request_headers["Authorization"], "<redacted>")

    def test_hop_records_scheme_host(self):
        a = "http://example.com:8080/a"
        b = "http://example.com:8080/b"
        tracer = RedirectTracer(make_fetch(chain(a, b)))
        result = tracer.trace(a)
        hop = result.hops[0]
        self.assertEqual((hop.scheme, hop.host, hop.port), ("http", "example.com", 8080))


class TestCrossProtocol(unittest.TestCase):
    def test_https_to_http_strips_credentials(self):
        a = "https://example.com/a"
        b = "http://example.com/b"
        tracer = RedirectTracer(make_fetch(chain(a, b)))
        result = tracer.trace(a, headers=AUTH)
        self.assertEqual(result.reason, REASON_OK)
        self.assertEqual(sorted(result.hops[0].stripped_headers),
                         ["Authorization", "Cookie"])
        self.assertFalse(result.hops[1].credentials_carried)
        self.assertNotIn("Authorization", result.hops[1].request_headers)
        self.assertIn("X-Request-Id", result.hops[1].request_headers)

    def test_http_to_https_strips_credentials(self):
        a = "http://example.com/a"
        b = "https://example.com/b"
        tracer = RedirectTracer(make_fetch(chain(a, b)))
        result = tracer.trace(a, headers=AUTH)
        self.assertEqual(sorted(result.hops[0].stripped_headers),
                         ["Authorization", "Cookie"])

    def test_cross_host_same_scheme_strips(self):
        a = "https://a.example.com/x"
        b = "https://b.example.com/y"
        tracer = RedirectTracer(make_fetch(chain(a, b)))
        result = tracer.trace(a, headers=AUTH)
        self.assertEqual(sorted(result.hops[0].stripped_headers),
                         ["Authorization", "Cookie"])

    def test_cross_port_strips(self):
        a = "http://example.com:8080/a"
        b = "http://example.com:9090/b"
        tracer = RedirectTracer(make_fetch(chain(a, b)))
        result = tracer.trace(a, headers=AUTH)
        self.assertEqual(sorted(result.hops[0].stripped_headers),
                         ["Authorization", "Cookie"])

    def test_userinfo_in_target_url_flagged(self):
        a = "https://example.com/a"
        b = "https://user:pass@evil.example.com/"
        tracer = RedirectTracer(make_fetch(chain(a, b)))
        result = tracer.trace(a, headers=AUTH)
        self.assertEqual(result.reason, REASON_OK)
        self.assertTrue(result.hops[1].url_has_userinfo)
        self.assertTrue(result.hops[1].credentials_carried)
        # 凭据头仍被剥离，泄漏风险只来自 URL 内嵌 userinfo
        self.assertEqual(sorted(result.hops[0].stripped_headers),
                         ["Authorization", "Cookie"])


class TestLoop(unittest.TestCase):
    def test_self_loop(self):
        a = "http://example.com/loop"
        tracer = RedirectTracer(make_fetch({a: (302, {"Location": a})}))
        result = tracer.trace(a)
        self.assertEqual(result.reason, REASON_LOOP)
        self.assertEqual(len(result.hops), 1)

    def test_two_node_loop(self):
        a = "http://example.com/a"
        b = "http://example.com/b"
        routes = {a: (302, {"Location": b}), b: (302, {"Location": a})}
        tracer = RedirectTracer(make_fetch(routes))
        result = tracer.trace(a)
        self.assertEqual(result.reason, REASON_LOOP)
        self.assertEqual(len(result.hops), 2)

    def test_loop_detected_after_normalization(self):
        # %7E 与 ~ 等价，规范化后应识别为同一地址成环
        a = "http://example.com/~u"
        routes = {a: (302, {"Location": "http://example.com/%7Eu"})}
        tracer = RedirectTracer(make_fetch(routes))
        result = tracer.trace(a)
        self.assertEqual(result.reason, REASON_LOOP)


class TestChainTooLong(unittest.TestCase):
    def test_long_chain_terminated(self):
        n = 50
        urls = [f"http://example.com/{i}" for i in range(n + 1)]
        routes = chain(*urls)
        tracer = RedirectTracer(make_fetch(routes), max_redirects=10)
        result = tracer.trace(urls[0])
        self.assertEqual(result.reason, REASON_CHAIN_TOO_LONG)
        self.assertLessEqual(result.depth, 11)
        self.assertIn("超过最大重定向数", result.error)

    def test_chain_exactly_at_limit_ok(self):
        urls = [f"http://example.com/{i}" for i in range(6)]
        tracer = RedirectTracer(make_fetch(chain(*urls)), max_redirects=10)
        result = tracer.trace(urls[0])
        self.assertEqual(result.reason, REASON_OK)
        self.assertEqual(result.depth, 5)


class TestInvalidTarget(unittest.TestCase):
    def test_unsupported_scheme(self):
        a = "http://example.com/a"
        routes = {a: (302, {"Location": "ftp://example.com/file"})}
        tracer = RedirectTracer(make_fetch(routes))
        result = tracer.trace(a)
        self.assertEqual(result.reason, REASON_INVALID_TARGET)

    def test_javascript_scheme(self):
        a = "http://example.com/a"
        routes = {a: (302, {"Location": "javascript:alert(1)"})}
        tracer = RedirectTracer(make_fetch(routes))
        result = tracer.trace(a)
        self.assertEqual(result.reason, REASON_INVALID_TARGET)

    def test_bad_port(self):
        a = "http://example.com/a"
        routes = {a: (302, {"Location": "http://example.com:99999/x"})}
        tracer = RedirectTracer(make_fetch(routes))
        result = tracer.trace(a)
        self.assertEqual(result.reason, REASON_INVALID_TARGET)

    def test_control_char_in_location(self):
        a = "http://example.com/a"
        routes = {a: (302, {"Location": "http://example.com/\x01x"})}
        tracer = RedirectTracer(make_fetch(routes))
        result = tracer.trace(a)
        self.assertEqual(result.reason, REASON_INVALID_TARGET)

    def test_invalid_start_url(self):
        tracer = RedirectTracer(make_fetch({}))
        result = tracer.trace("ht!tp://")
        self.assertEqual(result.reason, REASON_INVALID_TARGET)

    def test_fetch_error(self):
        def boom(url, headers):
            raise ConnectionError("refused")
        tracer = RedirectTracer(boom)
        result = tracer.trace("http://example.com/")
        self.assertEqual(result.reason, REASON_FETCH_ERROR)


class TestRelativeAndEscaped(unittest.TestCase):
    def test_relative_redirect(self):
        a = "http://example.com/dir/page"
        routes = {a: (302, {"Location": "../other?q=1"}),
                  "http://example.com/other?q=1": (200, {})}
        tracer = RedirectTracer(make_fetch(routes))
        result = tracer.trace(a)
        self.assertEqual(result.reason, REASON_OK)
        self.assertEqual(result.hops[0].resolved_next,
                         "http://example.com/other?q=1")

    def test_root_relative(self):
        a = "http://example.com/a/b/c"
        routes = {a: (301, {"Location": "/top"}),
                  "http://example.com/top": (200, {})}
        tracer = RedirectTracer(make_fetch(routes))
        result = tracer.trace(a)
        self.assertEqual(result.hops[0].resolved_next, "http://example.com/top")

    def test_scheme_relative(self):
        a = "http://example.com/a"
        routes = {a: (302, {"Location": "//cdn.example.com/x"}),
                  "http://cdn.example.com/x": (200, {})}
        tracer = RedirectTracer(make_fetch(routes))
        result = tracer.trace(a)
        self.assertEqual(result.hops[0].resolved_next,
                         "http://cdn.example.com/x")

    def test_dot_segments_normalized(self):
        self.assertEqual(normalize_url("http://example.com/a/./b/../c"),
                         "http://example.com/a/c")

    def test_percent_encoding_normalized(self):
        self.assertEqual(normalize_url("http://example.com/%7euser/%2F"),
                         "http://example.com/~user/%2F")

    def test_default_port_and_case_normalized(self):
        self.assertEqual(normalize_url("HTTP://EXAMPLE.COM:80/A"),
                         "http://example.com/A")
        self.assertEqual(normalize_url("https://example.com:443/"),
                         "https://example.com/")

    def test_fragment_dropped(self):
        self.assertEqual(normalize_url("http://example.com/a#frag"),
                         "http://example.com/a")

    def test_resolve_returns_details(self):
        info = resolve_url("http://example.com/a/b", "../c%2fd")
        self.assertEqual(info["normalized"], "http://example.com/c%2Fd")
        self.assertEqual(info["scheme"], "http")
        self.assertEqual(info["host"], "example.com")
        self.assertFalse(info["has_userinfo"])

    def test_same_origin(self):
        self.assertTrue(is_same_origin("http://example.com/a",
                                       "http://example.com:80/b"))
        self.assertFalse(is_same_origin("http://example.com/a",
                                        "https://example.com/a"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
