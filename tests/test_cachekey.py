"""Self-tests for cachekey: equivalence pairs, counterexamples, framing,
length bound, and injectivity.  Run:  python3 -m unittest discover -s tests -v
"""

import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cachekey import (
    HeaderError,
    KeyBuilder,
    KeyTooLongError,
    build_cache_key,
)

DATA = os.path.join(os.path.dirname(__file__), "..", "data")


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        return json.load(fh)


def builder_for(case):
    return KeyBuilder(
        participating=case.get("participating", None) or KeyBuilder().participating,
        policies=case.get("policies"),
    )


class TestEquivalentPairs(unittest.TestCase):
    def test_equivalent_pairs_share_one_key(self):
        for case in load("equivalent_pairs.json")["pairs"]:
            with self.subTest(id=case["id"]):
                b = builder_for(case)
                ka = b.build(case["a"])
                kb = b.build(case["b"])
                self.assertEqual(ka.key, kb.key, case["why"])
                self.assertEqual(ka.canonical, kb.canonical)


class TestCounterexamples(unittest.TestCase):
    def test_semantically_different_requests_never_merge(self):
        for case in load("counterexamples.json")["pairs"]:
            with self.subTest(id=case["id"]):
                b = builder_for(case)
                ka = b.build(case["a"])
                kb = b.build(case["b"])
                self.assertNotEqual(ka.key, kb.key, case["why"])
                self.assertNotEqual(ka.canonical, kb.canonical)


class TestFramingInjection(unittest.TestCase):
    """The length-framed encoding must not be confused by delimiter bytes."""

    def test_delimiter_bytes_in_values_do_not_collide(self):
        b = KeyBuilder(participating=["x-a", "x-b"])
        k1 = b.build([("x-a", "bc"), ("x-b", "")])
        k2 = b.build([("x-a", "b"), ("x-b", "c")])
        self.assertNotEqual(k1.key, k2.key)

    def test_netstring_lookalike_value(self):
        b = KeyBuilder(participating=["x-a", "x-b"])
        k1 = b.build([("x-a", "1:c,")])
        k2 = b.build([("x-a", "c"), ("x-b", "1:,")])
        self.assertNotEqual(k1.key, k2.key)

    def test_colon_comma_in_raw_value(self):
        b = KeyBuilder(participating=["x-a"])
        k1 = b.build([("x-a", "3:abc,")])
        k2 = b.build([("x-a", "3:abc")])
        self.assertNotEqual(k1.key, k2.key)


class TestLengthBound(unittest.TestCase):
    def test_direct_key_length_bound(self):
        b = KeyBuilder()  # canonical <= 4096 bytes
        key = b.build([("Accept", "application/json")])
        self.assertFalse(key.hashed)
        self.assertLessEqual(key.length, 4 + ((4096 * 4 + 2) // 3))

    def test_overlong_value_is_hashed_not_truncated(self):
        b = KeyBuilder()
        v1 = "a" * 10000
        v2 = "a" * 9999 + "b"
        k1 = b.build([("Cookie", "x=" + v1)])
        k2 = b.build([("Cookie", "x=" + v2)])
        self.assertTrue(k1.hashed)
        self.assertTrue(k2.hashed)
        self.assertEqual(k1.length, len("HK1H$") + 43)  # fixed digest length
        self.assertNotEqual(k1.key, k2.key)  # not silently truncated together

    def test_overlong_equivalent_forms_still_merge(self):
        b = KeyBuilder()
        v = "y=" + "z" * 9000
        k1 = b.build([("Cookie", v)])
        k2 = b.build([("cookie", "  " + v + "\t")])
        self.assertEqual(k1.key, k2.key)

    def test_reject_policy(self):
        b = KeyBuilder(on_too_long="reject")
        with self.assertRaises(KeyTooLongError):
            b.build([("Cookie", "x=" + "a" * 10000)])

    def test_hashed_and_direct_namespaces_never_collide(self):
        b = KeyBuilder()
        direct = b.build([("Accept", "x")])
        hashed = b.build([("Cookie", "x=" + "a" * 10000)])
        self.assertTrue(direct.key.startswith("HK1$"))
        self.assertTrue(hashed.key.startswith("HK1H$"))
        self.assertNotEqual(direct.key, hashed.key)


class TestValidation(unittest.TestCase):
    def test_crlf_nul_rejected(self):
        for bad in ["a\rb", "a\nb", "a\x00b", "a\r\n b"]:
            with self.subTest(bad=bad):
                with self.assertRaises(HeaderError):
                    build_cache_key([("Cookie", bad)])

    def test_bad_header_names_rejected(self):
        for bad in ["", "bad name", "bad:name", "bad\x01name"]:
            with self.subTest(bad=bad):
                with self.assertRaises(HeaderError):
                    build_cache_key([(bad, "x")])

    def test_bytes_input(self):
        k1 = build_cache_key([(b"Accept", b"application/json")])
        k2 = build_cache_key([("accept", "application/json")])
        self.assertEqual(k1.key, k2.key)


class TestPolicies(unittest.TestCase):
    def test_token_set_dedup(self):
        b = KeyBuilder()
        k1 = b.build([("Accept-Encoding", "gzip, gzip, br")])
        k2 = b.build([("Accept-Encoding", "br, gzip")])
        self.assertEqual(k1.key, k2.key)

    def test_token_list_keeps_duplicates(self):
        b = KeyBuilder()
        k1 = b.build([("Accept", "text/html, text/html")])
        k2 = b.build([("Accept", "text/html")])
        self.assertNotEqual(k1.key, k2.key)

    def test_raw_preserves_inner_whitespace(self):
        b = KeyBuilder()
        k1 = b.build([("Cookie", "a=1;  b=2")])
        k2 = b.build([("Cookie", "a=1; b=2")])
        self.assertNotEqual(k1.key, k2.key)

    def test_quoted_string_case_preserved_in_token_list(self):
        b = KeyBuilder(participating=["x-t"], policies={"x-t": "token_list"})
        k1 = b.build([("x-t", 'token, "MiXeD"')])
        k2 = b.build([("x-t", 'TOKEN, "MiXeD"')])
        k3 = b.build([("x-t", 'token, "mixed"')])
        self.assertEqual(k1.key, k2.key)
        self.assertNotEqual(k1.key, k3.key)


class TestInjectivityFuzz(unittest.TestCase):
    """Decode canonical bytes back to records; encoding must be a bijection
    between normalized record tuples and canonical bytes."""

    @staticmethod
    def decode(canonical: bytes):
        assert canonical[:4] == b"HK1,"
        pos = 4
        comma = canonical.index(b",", pos)
        nrec = int(canonical[pos:comma])
        pos = comma + 1
        records = []
        for _ in range(nrec):
            colon = canonical.index(b":", pos)
            n = int(canonical[pos:colon])
            pos = colon + 1
            name = canonical[pos : pos + n]
            pos += n
            assert canonical[pos : pos + 1] == b","
            pos += 1
            flag = canonical[pos : pos + 1]
            pos += 1
            assert canonical[pos : pos + 1] == b","
            pos += 1
            comma = canonical.index(b",", pos)
            nelem = int(canonical[pos:comma])
            pos = comma + 1
            elems = []
            for _ in range(nelem):
                colon = canonical.index(b":", pos)
                n = int(canonical[pos:colon])
                pos = colon + 1
                elems.append(canonical[pos : pos + n])
                pos += n
                assert canonical[pos : pos + 1] == b","
                pos += 1
            records.append((name, flag, tuple(elems)))
        assert pos == len(canonical)
        return tuple(records)

    @staticmethod
    def encode_records(records):
        """Independent reference encoder (netstring framing)."""
        out = bytearray(b"HK1,")
        out += str(len(records)).encode() + b","
        for name, flag, elems in records:
            out += str(len(name)).encode() + b":" + name + b","
            out += flag + b"," + str(len(elems)).encode() + b","
            for elem in elems:
                out += str(len(elem)).encode() + b":" + elem + b","
        return bytes(out)

    def test_random_requests_roundtrip_and_injective(self):
        # decode(encode(x)) must round-trip: the decoder is a left inverse of
        # the encoder, which proves the encoding is injective — two different
        # normalized record tuples can never serialize to the same bytes.
        rng = random.Random(20261005)
        names = ["accept", "accept-encoding", "cookie", "content-type",
                 "authorization", "accept-language"]
        alphabet = "ab,\":; =\tGZ19"
        b = KeyBuilder()
        canonicals = set()
        for _ in range(4000):
            headers = []
            for name in names:
                if rng.random() < 0.6:
                    value = "".join(rng.choice(alphabet)
                                    for _ in range(rng.randint(0, 12)))
                    if '"' in value:
                        value = value.replace('"', "")  # keep grammar valid
                    headers.append((name, value))
            key = b.build(headers)
            decoded = self.decode(key.canonical)
            self.assertEqual(self.encode_records(decoded), key.canonical)
            canonicals.add(key.canonical)
        # distinct decoded records always produced distinct canonical bytes
        self.assertEqual(len(canonicals),
                         len({self.encode_records(self.decode(c))
                              for c in canonicals}))


if __name__ == "__main__":
    unittest.main()
