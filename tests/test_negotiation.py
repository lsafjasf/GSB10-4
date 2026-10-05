"""Unit tests for Accept-Encoding / Content-Encoding negotiation."""

import unittest

from codec import (
    HeaderParseError,
    NegotiationError,
    build_accept_header,
    negotiate,
    parse_accept_encoding,
    parse_content_encoding,
)


class ParseTests(unittest.TestCase):
    def test_content_encoding_parse(self):
        self.assertEqual(parse_content_encoding(None), [])
        self.assertEqual(parse_content_encoding("gzip"), ["gzip"])
        self.assertEqual(parse_content_encoding(" GZIP , x-gzip "),
                         ["gzip", "gzip"])
        self.assertEqual(parse_content_encoding("gzip, deflate"),
                         ["gzip", "deflate"])

    def test_malformed_content_encoding(self):
        for bad in ("gzip,", ",gzip", "gzip; q=1", "gzip deflate", 'gzip, "x"'):
            with self.assertRaises(HeaderParseError):
                parse_content_encoding(bad)

    def test_accept_encoding_parse(self):
        self.assertEqual(parse_accept_encoding(None), {})
        self.assertEqual(parse_accept_encoding("gzip, deflate;q=0.5"),
                         {"gzip": 1.0, "deflate": 0.5})
        self.assertEqual(parse_accept_encoding("*;q=0.1, br;q=0"),
                         {"*": 0.1, "br": 0.0})

    def test_bad_qvalue(self):
        for bad in ("gzip;q=2", "gzip;q=x", "gzip;q=", "gzip;"):
            with self.assertRaises(HeaderParseError):
                parse_accept_encoding(bad)


class NegotiateTests(unittest.TestCase):
    def test_prefers_supported_order(self):
        self.assertEqual(negotiate("gzip, deflate"), "gzip")
        self.assertEqual(negotiate("gzip;q=0.1, deflate"), "deflate")

    def test_wildcard(self):
        self.assertEqual(negotiate("*"), "gzip")

    def test_absent_header_means_identity(self):
        self.assertEqual(negotiate(None), "identity")
        self.assertEqual(negotiate(""), "identity")

    def test_refuse_identity(self):
        with self.assertRaises(NegotiationError):
            negotiate("identity;q=0, gzip;q=0, deflate;q=0, *;q=0")

    def test_identity_default_acceptable(self):
        # Only identity is acceptable even though not listed.
        self.assertEqual(
            negotiate("gzip;q=0, deflate;q=0",
                      supported=("gzip", "identity")),
            "identity",
        )

    def test_zero_explicit_refused(self):
        with self.assertRaises(NegotiationError):
            negotiate("gzip;q=0", supported=("gzip",))

    def test_build_header_is_parseable(self):
        parsed = parse_accept_encoding(build_accept_header())
        self.assertIn("gzip", parsed)


if __name__ == "__main__":
    unittest.main()
