"""100 万顶点解析基准: 耗时与内存峰值, 峰值不得是文件大小的多倍。

构建阶段流式写盘(不持有整块大缓冲), 随后读入解析并全量遍历,
这样进程峰值主要就是解析阶段持有的一份文件字节。

用法: python3 bench.py [vertex_count]
"""

import os
import random
import resource
import struct
import sys
import tempfile
import time

from vxl.format import HEADER_FMT, ATTR_FMT, MAGIC, LAYOUT_INTERLEAVED
from vxl.reader import Mesh

ATTRS = [("position", 3), ("normal", 3), ("uv", 2)]  # 8 floats = 32 字节/顶点


def peak_rss_bytes():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024  # Linux: KB


def build_file(path, n):
    """流式生成 n 个顶点的交错布局文件, 每次只缓冲一个小块。"""
    rng = random.Random(42)
    stride = 32
    header = struct.pack(HEADER_FMT, MAGIC, n, len(ATTRS), n * 3, 3, b"\0" * 3)
    records = b""
    offset = 0
    for name, comps in ATTRS:
        encoded = name.encode("ascii")
        records += struct.pack(
            ATTR_FMT, encoded + b"\0" * (16 - len(encoded)),
            LAYOUT_INTERLEAVED, comps, 0, 0, stride, offset, 0)
        offset += comps * 4
    chunk = 65536
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(records)
        for start in range(0, n, chunk):
            count = min(chunk, n - start)
            vals = [rng.uniform(-1.0, 1.0) for _ in range(count * 8)]
            fh.write(struct.pack("<%df" % len(vals), *vals))
        for start in range(0, n, chunk):
            count = min(chunk, n - start)
            idx = [v for i in range(start, start + count)
                   for v in (i % n, (i + 1) % n, (i + 2) % n)]
            fh.write(struct.pack("<%dI" % len(idx), *idx))


def main():
    # 生成放到子进程, 本进程只负责解析, 峰值不受构建阶段影响
    if len(sys.argv) >= 4 and sys.argv[1] == "--gen":
        build_file(sys.argv[2], int(sys.argv[3]))
        return 0
    import subprocess
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    fd, path = tempfile.mkstemp(suffix=".vxb")
    os.close(fd)
    try:
        print("building %d-vertex file (streaming to disk) ..." % n, flush=True)
        subprocess.check_call([sys.executable, os.path.abspath(__file__),
                               "--gen", path, str(n)])
        file_size = os.path.getsize(path)

        rss_before = peak_rss_bytes()
        t0 = time.perf_counter()
        with open(path, "rb") as fh:
            raw = fh.read()
        mesh = Mesh(raw)
        t1 = time.perf_counter()

        # 全量遍历顶点与索引, 强制按步长/偏移取数
        checksum = 0.0
        for name, _comps in ATTRS:
            acc = mesh.attribute(name)
            for v in acc:
                checksum += v[0]
        index_sum = 0
        for prim in mesh.primitives():
            index_sum += prim[0]
        t2 = time.perf_counter()
        rss_after = peak_rss_bytes()

        delta = max(0, rss_after - rss_before)
        ratio = delta / file_size if file_size else 0.0
        print("file size:            %12d bytes (%.1f MiB)"
              % (file_size, file_size / 2**20))
        print("parse time:           %12.3f s" % (t1 - t0))
        print("iterate time:         %12.3f s" % (t2 - t1))
        print("total time:           %12.3f s" % (t2 - t0))
        print("peak RSS before:      %12d bytes (%.1f MiB)"
              % (rss_before, rss_before / 2**20))
        print("peak RSS after:       %12d bytes (%.1f MiB)"
              % (rss_after, rss_after / 2**20))
        print("peak RSS delta:       %12d bytes (%.1f MiB)"
              % (delta, delta / 2**20))
        print("delta / file size:    %12.2f x" % ratio)
        print("checksum=%.6f index_sum=%d" % (checksum, index_sum))
        ok = ratio < 1.5
        print("PASS: peak memory stays within 1.5x of file size" if ok
              else "FAIL: peak memory is a multiple of file size")
        return 0 if ok else 1
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
