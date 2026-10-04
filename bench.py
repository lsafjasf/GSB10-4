"""1,000,000-vertex parse benchmark: timing and peak memory.

Run: python3 bench.py
Stdlib only. Peak RSS must stay below 2x the file size.

Structure: the fixture file is built in one helper process and measured in
another, so the parent's RSS never pollutes the child's ru_maxrss
(hiwater RSS is inherited across fork+exec on Linux).
"""

import json
import os
import struct
import subprocess
import sys
import tempfile
from array import array

N = 1_000_000
HERE = os.path.dirname(os.path.abspath(__file__))


def build_file(path):
    """1M vertices, interleaved position(3)+normal(3)+uv(2), stride 32."""
    from vertex_layout import (
        Attribute, Model, Primitive, LAYOUT_INTERLEAVED, PRIM_TRIANGLES,
    )
    rec8 = struct.Struct("<8f")
    blob = bytearray(N * 32)
    mv = memoryview(blob)
    for i in range(N):
        rec8.pack_into(
            mv, i * 32,
            i * 0.5, i * 0.25 - 3.0, 2.0 - i,
            0.0, 1.0, 0.0,
            (i % 1024) / 1024.0, (i % 512) / 512.0)
    indices = array("I")
    for t in range(N // 3):
        indices.extend((3 * t, 3 * t + 1, 3 * t + 2))
    attrs = [
        Attribute("position", 3, LAYOUT_INTERLEAVED, 32, 0),
        Attribute("normal", 3, LAYOUT_INTERLEAVED, 32, 12),
        Attribute("uv", 2, LAYOUT_INTERLEAVED, 32, 24),
    ]
    raw = Model(N, attrs, [Primitive(PRIM_TRIANGLES, indices)], bytes(blob)).serialize()
    with open(path, "wb") as f:
        f.write(raw)


RUN_CHILD = r"""
import json, resource, sys, time
from vertex_layout import parse
def rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
base = rss()
with open(sys.argv[1], "rb") as f:
    raw = f.read()
t0 = time.perf_counter()
model = parse(raw)
t1 = time.perf_counter()
checksum = 0.0
for name in ("position", "normal", "uv"):
    for row in model.iter_attr(name):
        checksum += row[0]
t2 = time.perf_counter()
print(json.dumps({"parse_ms": (t1 - t0) * 1000,
                  "scan_ms": (t2 - t1) * 1000,
                  "checksum": checksum,
                  "base_rss": base,
                  "peak_rss": rss()}))
"""


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--build":
        build_file(sys.argv[2])
        return

    fd, path = tempfile.mkstemp(prefix="vxl-bench-", suffix=".vxl")
    os.close(fd)
    try:
        subprocess.run([sys.executable, __file__, "--build", path],
                       cwd=HERE, check=True)
        file_size = os.path.getsize(path)
        proc = subprocess.run([sys.executable, "-c", RUN_CHILD, path],
                              cwd=HERE, capture_output=True, text=True, check=True)
        stats = json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)

    growth = stats["peak_rss"] - stats["base_rss"]
    ratio = growth / file_size
    print("vertices:        %d (interleaved stride 32, 333333 triangles)" % N)
    print("file size:       %d bytes (%.2f MiB)" % (file_size, file_size / 2**20))
    print("parse time:      %.1f ms (fresh process, no tracer)" % stats["parse_ms"])
    print("full scan time:  %.1f ms (all components of all vertices)" % stats["scan_ms"])
    print("baseline RSS:    %.2f MiB (interpreter, before reading the file)"
          % (stats["base_rss"] / 2**20))
    print("peak RSS:        %.2f MiB (file bytes + zero-copy parsed model)"
          % (stats["peak_rss"] / 2**20))
    print("RSS growth:      %.2f MiB" % (growth / 2**20))
    print("growth/file:     %.2fx" % ratio)
    print("checksum:        %.3f" % stats["checksum"])
    assert ratio < 2.0, "peak memory growth %.2fx of file size >= 2.0x" % ratio
    print("PASS: peak memory growth is below 2x the file size (zero-copy views)")


if __name__ == "__main__":
    main()
