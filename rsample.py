#!/usr/bin/env python3
"""可复现的键哈希随机采样（仅标准库）。

算法（详见 README.md）:
  h(key) = BLAKE2b-64(key, keyed_by_seed)          # 64 位无符号整数
  保留   <=> h(key) < floor(rate * 2**64)

性质:
  - 给定 (seed, rate, key) 结果完全确定，跨平台/跨语言实现一致（BLAKE2 是标准算法）；
  - 逐键独立判定，与输入顺序、所在分片无关；
  - 分片采样的并集与单机整体采样逐行相同（不只是统计一致）。
"""

import argparse
import hashlib
import resource
import struct
import sys

MOD = 1 << 64
_KEYED_HASH = "blake2b"


def _seed_bytes(seed):
    """把任意种子规范化为 BLAKE2 key（<=64 字节），保持确定性。"""
    if isinstance(seed, int):
        return struct.pack("<Q", seed % MOD)
    if isinstance(seed, str):
        raw = seed.encode("utf-8")
    elif isinstance(seed, bytes):
        raw = seed
    else:
        raise TypeError("seed 必须是 int / str / bytes")
    if len(raw) > 64:  # BLAKE2b key 上限 64 字节
        raw = hashlib.blake2b(raw, digest_size=64).digest()
    return raw


class HashSampler:
    """逐键伯努利采样器：keep(key) 为 True 表示该行入样。

    seed 决定使用哪一张固定的“随机表”，rate 是采样率阈值。
    同一个 (seed, key) 在任意 rate 下哈希值相同；调小 rate 是调小阈值，
    因此嵌套抽样（低采样率样本是高采样率样本的子集）也成立。
    """

    def __init__(self, seed, rate):
        if not 0.0 <= rate <= 1.0:
            raise ValueError("rate 必须在 [0, 1]")
        self.seed = _seed_bytes(seed)
        self.rate = rate
        # floor 保证 rate=1 时阈值 == 2^64，全部入样
        self.threshold = int(rate * MOD)
        if rate == 1.0:
            self.threshold = MOD

    def hash64(self, key):
        """返回 key 在当前 seed 下的 64 位稳定哈希。"""
        if isinstance(key, str):
            key = key.encode("utf-8")
        h = hashlib.blake2b(digest_size=8, key=self.seed)
        h.update(key)
        return int.from_bytes(h.digest(), "little")

    def keep(self, key):
        return self.hash64(key) < self.threshold

    def filter(self, iterable):
        """流式过滤：不保留全量数据，内存 O(命中数由调用方决定)。"""
        keep = self.keep
        for key in iterable:
            if keep(key):
                yield key


def sample_stream(lines, seed, rate):
    """便捷函数：对字节行/字符串行迭代器做流式采样。"""
    return HashSampler(seed, rate).filter(lines)


def _iter_stdin(buf):
    for line in buf:
        if line.endswith(b"\n"):
            line = line[:-1]
            if line.endswith(b"\r"):
                line = line[:-1]
        yield line


def main(argv=None):
    parser = argparse.ArgumentParser(description="可复现的键哈希随机采样")
    parser.add_argument("--seed", required=True, help="整数或字符串种子")
    parser.add_argument("--rate", required=True, type=float, help="采样率 [0,1]")
    parser.add_argument("--input", default="-", help="输入文件，默认 stdin")
    parser.add_argument("--output", default="-", help="输出文件，默认 stdout")
    args = parser.parse_args(argv)

    seed = args.seed
    try:
        seed = int(seed)
    except ValueError:
        pass
    sampler = HashSampler(seed, args.rate)

    fin = sys.stdin.buffer if args.input == "-" else open(args.input, "rb")
    fout = sys.stdout.buffer if args.output == "-" else open(args.output, "wb")
    try:
        for line in _iter_stdin(fin):
            if sampler.keep(line):
                fout.write(line)
                fout.write(b"\n")
    finally:
        if fin is not sys.stdin.buffer:
            fin.close()
        if fout is not sys.stdout.buffer:
            fout.close()


if __name__ == "__main__":
    main()
