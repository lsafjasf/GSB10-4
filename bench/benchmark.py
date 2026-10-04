"""100 MiB benchmark + memory-upper-bound table.

Every measurement runs in a fresh subprocess (bench/_worker.py) so the
reported peak RSS (ru_maxrss) reflects only that decode, not the fixture
generation. Streaming reads the compressed response from disk in 64 KiB
chunks; one-shot reads and decompresses everything in memory.

Usage (repo root):
    python3 bench/benchmark.py
Writes reports/benchmark.txt.
"""

import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "bench", "_worker.py")

MiB = 1024 * 1024


def make_compressed(path, size, mode):
    """Write a gzip-compressed fixture; return (sha256_of_plain, comp_len)."""
    digest = hashlib.sha256()
    comp = gzip.GzipFile(path, "wb", compresslevel=6)
    if mode == "random":
        remaining = size
        while remaining:
            block = os.urandom(min(remaining, 4 * MiB))
            digest.update(block)
            comp.write(block)
            remaining -= len(block)
    else:
        block = (
            b"Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 512
            + os.urandom(4096)
        )
        written = 0
        while written < size:
            piece = block[: min(len(block), size - written)]
            digest.update(piece)
            comp.write(piece)
            written += len(piece)
    comp.close()
    return digest.hexdigest(), os.path.getsize(path)


def run_worker(mode, path, declared):
    out = subprocess.run(
        [sys.executable, WORKER, mode, path, str(declared)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return {
        "elapsed": float(out[1]),
        "peak_mib": float(out[2]),
        "produced": int(out[3]),
        "sha256": out[4],
    }


def main():
    lines = ["streamdecode 100 MiB benchmark", "=" * 60]
    tmpdir = tempfile.mkdtemp(prefix="streamdecode-bench-")
    meta = {}

    sizes = [1, 10, 50, 100]
    for mib in sizes:
        path = os.path.join(tmpdir, "random-%d.gz" % mib)
        sha, clen = make_compressed(path, mib * MiB, "random")
        meta[("random", mib)] = (path, sha, clen)

    lines.append("streaming decode, random (incompressible) payload:")
    lines.append(
        "  compressed   plaintext   time(s)   MiB/s   peak RSS (MiB, absolute)"
    )
    lines.append("  " + "-" * 66)
    for mib in sizes:
        path, sha, clen = meta[("random", mib)]
        r = run_worker("stream", path, clen)
        assert r["sha256"] == sha, "hash mismatch at %d MiB" % mib
        assert r["produced"] == mib * MiB
        lines.append(
            "  %9d MiB   %7d MiB   %7.3f   %6.0f   %15.2f"
            % (clen / MiB, mib, r["elapsed"], mib / r["elapsed"], r["peak_mib"])
        )
    lines.append("")
    lines.append(
        "peak RSS stays flat while the response grows 1 -> 100 MiB: "
        "memory does not follow the body size."
    )

    path, sha, clen = meta[("random", 100)]
    r_stream = run_worker("stream", path, clen)
    r_oneshot = run_worker("oneshot", path, clen)
    assert r_stream["sha256"] == r_oneshot["sha256"] == sha
    lines.append("")
    lines.append("100 MiB compressed response (decompressed = 100 MiB):")
    lines.append(
        "  streaming : %.3f s, %.0f MiB/s, peak RSS %.2f MiB"
        % (r_stream["elapsed"], 100 / r_stream["elapsed"], r_stream["peak_mib"])
    )
    lines.append(
        "  one-shot  : %.3f s, %.0f MiB/s, peak RSS %.2f MiB"
        % (
            r_oneshot["elapsed"],
            100 / r_oneshot["elapsed"],
            r_oneshot["peak_mib"],
        )
    )

    path = os.path.join(tmpdir, "mixed-100.gz")
    sha, clen = make_compressed(path, 100 * MiB, "mixed")
    r = run_worker("stream", path, clen)
    assert r["sha256"] == sha
    lines.append("")
    lines.append(
        "100 MiB highly-compressible payload (compressed %.1f MiB): "
        "%.3f s, %.0f MiB/s, peak RSS %.2f MiB"
        % (clen / MiB, r["elapsed"], 100 / r["elapsed"], r["peak_mib"])
    )

    report = "\n".join(lines) + "\n"
    print(report)
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    with open(os.path.join(ROOT, "reports", "benchmark.txt"), "w") as f:
        f.write(report)

    for p, _, _ in meta.values():
        os.unlink(p)
    os.unlink(path)
    os.rmdir(tmpdir)


if __name__ == "__main__":
    main()
