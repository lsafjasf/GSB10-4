#!/usr/bin/env python3
"""自测：对拍、反例、注入性、错误分支、幂等性、长度上界。

运行：python3 tests/run_tests.py
"""
import json
import os
import random
import string
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cachekey  # noqa: E402
from cachekey import (  # noqa: E402
    Policy,
    canonical,
    key,
    normalize_value,
    HeaderBlockTooLarge,
    InvalidHeaderName,
    InvalidHeaderValue,
    MAX_KEY_LEN,
)

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# 对拍数据载入
# ---------------------------------------------------------------------------


def _policies_from(spec):
    if not spec:
        return None
    out = dict(cachekey.DEFAULT_POLICIES)
    for name, kwargs in spec.items():
        out[name.lower()] = Policy(**kwargs)
    return out


def _split_request(req, group_vary, group_policies):
    """支持 dict / [[name,value],...]；_vary/_policies 可逐条覆盖。"""
    if isinstance(req, dict):
        vary = req.get("_vary", group_vary)
        policies = _policies_from(req.get("_policies", group_policies))
        headers = [(k, v) for k, v in req.items() if not k.startswith("_")]
    else:
        vary, policies, headers = group_vary, group_policies, req
    return headers, vary, policies or _policies_from(None)


with open(os.path.join(HERE, "pairs.json"), encoding="utf-8") as fh:
    PAIRS = json.load(fh)


class PairsTest(unittest.TestCase):
    def test_equivalent_groups_collapse_to_one_key(self):
        for group in PAIRS["equivalent"]:
            with self.subTest(desc=group["desc"]):
                keys = set()
                canons = set()
                for req in group["requests"]:
                    headers, vary, policies = _split_request(
                        req, group["vary"], group.get("policies")
                    )
                    canons.add(canonical(headers, vary, policies))
                    keys.add(key(headers, vary, policies))
                self.assertEqual(len(canons), 1, f"等价请求 canonical 不一致: {group['desc']}")
                self.assertEqual(len(keys), 1, f"等价请求 key 不一致: {group['desc']}")

    def test_distinct_pairs_never_merge(self):
        for group in PAIRS["distinct"]:
            with self.subTest(desc=group["desc"]):
                keys = set()
                canons = set()
                for req in group["requests"]:
                    headers, vary, policies = _split_request(
                        req, group["vary"], group.get("policies")
                    )
                    canons.add(canonical(headers, vary, policies))
                    keys.add(key(headers, vary, policies))
                self.assertEqual(
                    len(keys), len(group["requests"]),
                    f"语义不同的请求被合并了: {group['desc']}",
                )
                self.assertEqual(len(canons), len(group["requests"]))


# ---------------------------------------------------------------------------
# 独立解码器：canonical -> (vary 指纹列表, 记录列表)，验证编码可逆（故注入）
# ---------------------------------------------------------------------------


class _Reader:
    def __init__(self, data):
        self.buf = data
        self.pos = 0

    def take(self, n):
        if self.pos + n > len(self.buf):
            raise ValueError("截断")
        chunk = self.buf[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def expect(self, expected):
        got = self.take(len(expected))
        if got != expected:
            raise ValueError(f"期望 {expected!r} 实际 {got!r}")

    def until(self, sep):
        end = self.buf.find(sep, self.pos)
        if end < 0:
            raise ValueError("找不到分隔符")
        chunk = self.buf[self.pos:end]
        self.pos = end + 1
        return chunk

    def length_prefixed(self):
        digits = self.until(b":")
        if not digits.isdigit():
            raise ValueError("长度前缀不是数字")
        return self.take(int(digits))

    def done(self):
        return self.pos == len(self.buf)


def decode_canonical(data: bytes):
    r = _Reader(data)
    r.expect(b"HK1\n")
    vary_fps = []
    while True:
        item = r.length_prefixed()
        vary_fps.append(item)
        if r.buf[r.pos:r.pos + 1] == b"\n":
            r.pos += 1
            break
    records = []
    while not r.done():
        flag = r.take(1)
        r.expect(b"\x00")
        if flag == b"0":
            records.append(None)
        elif flag == b"1":
            records.append(r.length_prefixed())
        else:
            raise ValueError("非法存在位")
    return vary_fps, records


def decode_key(k: str) -> bytes:
    if k.startswith("h1:"):
        # 哈希键不可逆，但可校验长度标签自洽
        rest = k[3:]
        length, digest = rest.split(":", 1)
        assert length.isdigit() and len(digest) == 64
        return b""
    assert k.startswith("v1:")
    body = k[3:]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "%":
            out.append(int(body[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(ord(ch))
            i += 1
    return bytes(out)


class InjectivityTest(unittest.TestCase):
    def test_length_prefix_is_injective_fuzz(self):
        """长度前缀拼接本身对任意字节（含 ':'、数字、\\x00）都必须无碰撞。"""
        rng = random.Random(20261005)
        seen = {}
        alphabet = b":0123456789x\x00 "
        for _ in range(3000):
            parts = tuple(bytes(rng.choice(alphabet) for _ in range(rng.randrange(0, 5)))
                          for _ in range(rng.randrange(1, 4)))
            encoded = b"".join(cachekey._lp(p) for p in parts)
            if encoded in seen:
                self.assertEqual(seen[encoded], parts,
                                 "不同输入产生相同编码 —— 长度前缀注入失败")
            seen[encoded] = parts

    def test_canonical_roundtrip_fuzz(self):
        """随机头集合：canonical 必须能被独立解码器无损还原。"""
        rng = random.Random(42)
        names = ["accept", "authorization", "x-a", "x-b"]
        alphabet = ["gzip", "deflate", "text/html", "a b", "a,b", "a;b",
                    "x=1", '"q, r"', "", " ", "ZZ", "a; b;c", ":", "\t"]
        seen_tuples = set()
        seen_enc = set()
        for _ in range(3000):
            records = {}
            for name in names:
                if rng.random() < 0.25:
                    continue  # 缺失
                records[name] = [rng.choice(alphabet) for _ in range(rng.randrange(1, 3))]
            headers = []
            for name, vals in records.items():
                for v in vals:
                    headers.append((name, v))
            rng.shuffle(headers)
            canon = canonical(headers, names)
            # 期望必须按打乱后的实际行序计算（顺序敏感头部乱序合法地产生不同键）
            ordered = {}
            for n, v in headers:
                ordered.setdefault(n, []).append(v)
            vary_fps, decoded = decode_canonical(canon)
            self.assertEqual(vary_fps, [
                n.encode() + b"\x00" + cachekey.DEFAULT_POLICIES.get(n, Policy()).fingerprint().encode()
                for n in names
            ])
            expect = []
            for name in names:
                if name not in ordered:
                    expect.append(None)
                else:
                    expect.append(normalize_value(name, ",".join(ordered[name])).encode())
            self.assertEqual(decoded, expect)
            sig = tuple(expect)
            self.assertEqual(sig in seen_tuples, canon in seen_enc)
            seen_tuples.add(sig)
            seen_enc.add(canon)

    def test_readable_key_roundtrip_and_collision_free(self):
        cases = [
            ({"Accept": "text/html"}, ["accept"]),
            ({"X-A": "a:b", "X-B": "1 2"}, ["x-a", "x-b"]),
            ({}, ["x-a"]),
            ({"X-A": ""}, ["x-a"]),
        ]
        seen = {}
        for headers, vary in cases * 50:
            k = key(headers, vary)
            self.assertTrue(k.startswith("v1:"))
            self.assertEqual(decode_key(k), canonical(headers, vary))
            seen.setdefault(k, canonical(headers, vary))
            self.assertEqual(seen[k], canonical(headers, vary))


class KeyLengthAndHashTest(unittest.TestCase):
    def test_all_keys_within_bound_including_long(self):
        rng = random.Random(7)
        for _ in range(500):
            n = rng.randrange(0, 4000)
            k = key({"X-Blob": "z" * n}, ["x-blob"])
            self.assertLessEqual(len(k), MAX_KEY_LEN, "键超过硬上界")

    def test_hash_key_never_truncates_and_tag_matches(self):
        big = {"X-Blob": "P" * 10000}
        canon = canonical(big, ["x-blob"])
        k = key(big, ["x-blob"])
        self.assertTrue(k.startswith("h1:"))
        self.assertEqual(k.split(":")[1], str(len(canon)))
        self.assertEqual(len(k), 3 + len(str(len(canon))) + 1 + 64)
        decode_key(k)  # 自洽校验

    def test_long_keys_distinct_for_distinct_inputs(self):
        a = key({"X-Blob": "X" + "q" * 3000 + "AAA"}, ["x-blob"])
        b = key({"X-Blob": "X" + "q" * 3000 + "BBB"}, ["x-blob"])
        self.assertNotEqual(a, b)
        # 尾部差异必须真的进入摘要：h1 键的摘要段必须不同
        self.assertNotEqual(a.split(":", 2)[2], b.split(":", 2)[2])

    def test_equivalent_long_inputs_same_hash(self):
        a = key({"X-Blob": "P" * 10000}, ["x-blob"])
        b = key([("x-blob", "  " + "P" * 10000 + "  ")], ["x-blob"])
        self.assertEqual(a, b)


class ErrorTest(unittest.TestCase):
    def test_bad_header_name(self):
        for bad in ["bad name", "x:y", "", "x\ty", "a\nb"]:
            with self.assertRaises(InvalidHeaderName):
                key({bad: "v"}, [bad if bad else "x"])

    def test_control_chars_rejected_not_normalized(self):
        with self.assertRaises(InvalidHeaderValue):
            key({"X-A": "a\nb"}, ["x-a"])
        with self.assertRaises(InvalidHeaderValue):
            key([("X-A", "a\r\nb")], ["x-a"])  # 非折叠的裸 CR/LF
        with self.assertRaises(InvalidHeaderValue):
            key({"X-A": "a\x00b"}, ["x-a"])

    def test_oversize_block_rejected_not_silently_truncated(self):
        with self.assertRaises(HeaderBlockTooLarge):
            key({"X-A": "y" * (cachekey.MAX_VARY_BLOCK + 1)}, ["x-a"])

    def test_empty_vary_rejected(self):
        with self.assertRaises(cachekey.CacheKeyError):
            key({"X-A": "1"}, [])

    def test_non_string_value_rejected(self):
        with self.assertRaises(InvalidHeaderValue):
            key([("X-A", 1)], ["x-a"])


class IdempotenceTest(unittest.TestCase):
    CASES = [
        ("accept", '  TEXT/HTML ;  Q = 1 , application/json; level=2 '),
        ("content-type", 'text/html; Charset=utf-8; title="a; b"; level=1'),
        ("authorization", "  Bearer x\ry"),
        ("x-raw", "  keep   spaces\tand Case  "),
        ("accept-encoding", "gzip,, deflate,"),
    ]

    def test_normalize_value_idempotent(self):
        for name, value in self.CASES:
            if "\r" in value or "\n" in value:
                with self.assertRaises(InvalidHeaderValue):
                    normalize_value(name, value)
                continue
            once = normalize_value(name, value)
            twice = normalize_value(name, once)
            self.assertEqual(once, twice, f"{name} 规范化不幂等")

    def test_key_deterministic_across_input_shapes(self):
        a = key({"Accept": "text/html", "Accept-Encoding": "gzip"}, ["accept", "accept-encoding"])
        b = key([("ACCEPT-ENCODING", "gzip"), ("accept", "text/html")],
                ["Accept-Encoding", "ACCEPT"])
        self.assertEqual(a, b)

    def test_all_ascii_and_single_line(self):
        """键必须是可直接用作存储键的单行 ASCII；'%' 只能是百分号编码前缀。"""
        samples = ["x", "a b", "u\U0001f600", "100%", "p:q", "a\tb"]
        for v in samples:
            k = key({"X-A": v}, ["x-a"])
            self.assertTrue(k.isascii())
            self.assertNotIn("\n", k)
            self.assertNotIn("\r", k)
            self.assertNotIn(" ", k)
            # 所有 % 必须形如 %XX（可逆性由 decode_key 单独验证）
            i = 0
            while i < len(k):
                if k[i] == "%":
                    int(k[i + 1:i + 3], 16)
                    i += 3
                else:
                    i += 1
        with self.assertRaises(InvalidHeaderValue):
            key({"X-A": "line\nbreak"}, ["x-a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
