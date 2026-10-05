"""Self-tests for redirect_trace. Run: python3 -m unittest -v test_redirect_trace"""

import unittest

from redirect_trace import (
    HttpFetcher,
    RedirectTracer,
    ScriptedFetcher,
    normalize_url,
)

AUTH = {"Authorization": "Bearer token", "Cookie": "sid=1", "Accept": "*/*"}


class TestNormalize(unittest.TestCase):
    def test_relative_resolution_and_dot_segments(self):
        n = normalize_url("../b/./c/../../x", base="https://a.test/dir/page")
        self.assertEqual(n.url, "https://a.test/x")

    def test_relative_root(self):
        n = normalize_url("/login", base="http://a.test/path?q=1")
        self.assertEqual(n.url, "http://a.test/login")

    def test_escaped_unreserved_and_dot(self):
        # %7e == '~' (unreserved) gets decoded; %2e%2e is ".." (reserved dot
        # encodings) and is still resolved as dot segments after decoding.
        n = normalize_url("https://a.test/a/%2e%2e/%7Eb")
        self.assertEqual(n.url, "https://a.test/~b")

    def test_percent_encoding_case_folded(self):
        n = normalize_url("https://a.test/a%2Fb?x=%41")
        self.assertEqual(n.url, "https://a.test/a%2Fb?x=A")

    def test_scheme_host_lowercase_and_default_port(self):
        n = normalize_url("HTTPS://A.TEST:443/")
        self.assertEqual(n.url, "https://a.test/")
        self.assertEqual(n.origin, ("https", "a.test", 443))

    def test_url_credentials_recorded(self):
        n = normalize_url("https://user:pw@a.test/")
        self.assertTrue(n.has_credentials)
        self.assertNotIn("user", n.url)

    def test_invalid_scheme(self):
        with self.assertRaisesRegex(Exception, "unsupported scheme"):
            normalize_url("javascript:alert(1)")

    def test_invalid_control_char(self):
        with self.assertRaisesRegex(Exception, "whitespace"):
            normalize_url("https://a.test/a b")

    def test_invalid_port(self):
        with self.assertRaisesRegex(Exception, "invalid port"):
            normalize_url("https://a.test:99999/")

    def test_missing_host(self):
        with self.assertRaisesRegex(Exception, "missing host"):
            normalize_url("https:/path")


class TestTracer(unittest.TestCase):
    def test_direct_redirect_keeps_credentials_same_origin(self):
        fetcher = ScriptedFetcher({
            "https://a.test/": (302, "https://a.test/home"),
            "https://a.test/home": (200, None),
        })
        result = RedirectTracer(fetcher).trace("https://a.test/", AUTH)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.depth, 2)
        self.assertEqual(result.hops[1].sensitive_headers_sent,
                         ["Authorization", "Cookie"])
        self.assertEqual(result.hops[1].sensitive_headers_stripped, [])

    def test_cross_host_strips_sensitive_headers(self):
        fetcher = ScriptedFetcher({
            "https://a.test/": (302, "https://b.test/home"),
            "https://b.test/home": (200, None),
        })
        result = RedirectTracer(fetcher).trace("https://a.test/", AUTH)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.hops[1].sensitive_headers_stripped,
                         ["Authorization", "Cookie"])
        # Non-sensitive header survives.
        self.assertEqual(fetcher.requests[1][1].get("Accept"), "*/*")
        self.assertNotIn("Authorization", fetcher.requests[1][1])

    def test_cross_scheme_downgrade_strips(self):
        fetcher = ScriptedFetcher({
            "https://a.test/": (301, "http://a.test/"),
            "http://a.test/": (200, None),
        })
        result = RedirectTracer(fetcher).trace("https://a.test/", AUTH)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.hops[1].scheme, "http")
        self.assertEqual(result.hops[1].sensitive_headers_stripped,
                         ["Authorization", "Cookie"])

    def test_cross_scheme_upgrade_then_back(self):
        fetcher = ScriptedFetcher({
            "http://a.test/": (302, "https://a.test/s"),
            "https://a.test/s": (302, "http://a.test/o"),
            "http://a.test/o": (200, None),
        })
        result = RedirectTracer(fetcher).trace("http://a.test/", AUTH)
        self.assertEqual(result.status, "ok")
        # Both origin changes strip; credentials are gone and never restored.
        self.assertTrue(result.hops[1].sensitive_headers_stripped)
        self.assertNotIn("Authorization", fetcher.requests[2][1])

    def test_cross_port_is_origin_change(self):
        fetcher = ScriptedFetcher({
            "https://a.test/": (302, "https://a.test:8443/"),
            "https://a.test:8443/": (200, None),
        })
        result = RedirectTracer(fetcher).trace("https://a.test/", AUTH)
        self.assertEqual(result.hops[1].sensitive_headers_stripped,
                         ["Authorization", "Cookie"])

    def test_loop_detected(self):
        fetcher = ScriptedFetcher({
            "https://a.test/1": (302, "https://a.test/2"),
            "https://a.test/2": (302, "https://a.test/1"),
        })
        result = RedirectTracer(fetcher, max_hops=20).trace(
            "https://a.test/1", AUTH)
        self.assertEqual(result.status, "loop")
        self.assertLessEqual(result.depth, 3)

    def test_loop_via_relative_url(self):
        fetcher = ScriptedFetcher({
            "https://a.test/a": (302, "/b"),
            "https://a.test/b": (302, "a"),
        })
        result = RedirectTracer(fetcher).trace("https://a.test/a")
        self.assertEqual(result.status, "loop")
        self.assertEqual(result.final_url, "https://a.test/a")

    def test_too_many_redirects(self):
        script = {f"https://a.test/{i}": (302, f"https://a.test/{i + 1}")
                  for i in range(100)}
        result = RedirectTracer(ScriptedFetcher(script), max_hops=5).trace(
            "https://a.test/0")
        self.assertEqual(result.status, "too_many_redirects")
        self.assertEqual(result.depth, 5)

    def test_invalid_redirect_target_reported(self):
        fetcher = ScriptedFetcher({
            "https://a.test/": (302, "javascript:alert(1)"),
        })
        result = RedirectTracer(fetcher).trace("https://a.test/")
        self.assertEqual(result.status, "invalid_url")
        self.assertIn("unsupported scheme", result.reason)

    def test_redirect_without_location_reported(self):
        fetcher = ScriptedFetcher({
            "https://a.test/": (302, None),
        })
        result = RedirectTracer(fetcher).trace("https://a.test/")
        self.assertEqual(result.status, "invalid_url")

    def test_invalid_start_url_reported(self):
        result = RedirectTracer(ScriptedFetcher({})).trace("ftp://a.test/")
        self.assertEqual(result.status, "invalid_url")
        self.assertEqual(result.depth, 0)

    def test_relative_redirect_chain_records(self):
        fetcher = ScriptedFetcher({
            "https://a.test/dir/page": (302, "next"),
            "https://a.test/dir/next": (302, "../final"),
            "https://a.test/final": (200, None),
        })
        result = RedirectTracer(fetcher).trace("https://a.test/dir/page")
        self.assertEqual(
            [h.url for h in result.hops],
            ["https://a.test/dir/page",
             "https://a.test/dir/next",
             "https://a.test/final"])
        self.assertEqual(result.status, "ok")

    def test_statuses_307_308_followed(self):
        fetcher = ScriptedFetcher({
            "https://a.test/": (307, "https://a.test/t"),
            "https://a.test/t": (308, "https://a.test/f"),
            "https://a.test/f": (200, None),
        })
        result = RedirectTracer(fetcher).trace("https://a.test/")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.depth, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
