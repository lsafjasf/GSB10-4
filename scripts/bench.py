#!/usr/bin/env python3
"""1,000,000 行合并基准：耗时 + 峰值内存，验证内存不随分片数线性增长。

用法:
  python3 scripts/bench.py all                 # 跑全部配置并打印表格
  python3 scripts/bench.py gen  <dir> <nshards>   # 生成 1M 行分片
  python3 scripts/bench.py run  <dir> <nshards>   # 合并并输出 JSON 指标
"""

from __future__ import annotations

import hashlib
import json
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from logshard import Merger, RotatingWriter, format_record  # noqa: E402

N_RECORDS = 1_000_000
BASE_TS = 1_700_000_000_000


def record_line(i: int) -> bytes:
    # 确定性负载：序号即 seq；每 1000 条一个同毫秒组；payload 60~90 字节
    ts = BASE_TS + (i // 1000) * 7 + i // 1000
    payload = (f"bench payload {i:07d} " + "x" * (40 + i % 30)).encode()
    return format_record(ts, i + 1, payload)


def expected_hash() -> str:
    h = hashlib.sha256()
    for i in range(N_RECORDS):
        h.update(record_line(i))
    return h.hexdigest()


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def cmd_gen(directory: Path, nshards: int) -> None:
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    total = sum(len(record_line(i)) for i in range(N_RECORDS))
    max_bytes = max(4096, -(-total // nshards) + 64)  # +64 吸收分片头，使目标分片数尽量精确
    t0 = time.perf_counter()
    with RotatingWriter(directory, max_bytes=max_bytes) as w:
        for i in range(N_RECORDS):
            w.write_line_bytes(record_line(i))
    actual = len(list(directory.iterdir()))
    print(f"gen: shards={actual} max_bytes={max_bytes} elapsed={time.perf_counter()-t0:.2f}s", file=sys.stderr)


def cmd_run(directory: Path, detect_duplicates: bool = True) -> None:
    want = expected_hash()
    files = [str(p) for p in directory.iterdir()]
    t0 = time.perf_counter()
    merger = Merger(files, detect_duplicates=detect_duplicates)
    h = hashlib.sha256()
    count = 0
    for rec in merger.records():
        h.update(rec.raw)
        count += 1
    elapsed = time.perf_counter() - t0
    report = merger.report
    out = {
        "shards": len(files),
        "detect_duplicates": detect_duplicates,
        "records": count,
        "elapsed_s": round(elapsed, 3),
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "hash_ok": h.hexdigest() == want,
        "gaps": len(report.gaps),
        "rollbacks": len(report.rollbacks),
        "dup_seqs": len(report.duplicate_seqs),
    }
    print(json.dumps(out))


def cmd_all() -> None:
    configs = [1, 64, 1024]
    rows = []
    tmp = Path(tempfile.mkdtemp(prefix="logshard-bench-"))
    try:
        for n in configs:
            d = tmp / f"shards-{n}"
            subprocess.run([sys.executable, __file__, "gen", str(d), str(n)], check=True)
            proc = subprocess.run(
                [sys.executable, __file__, "run", str(d)], check=True, capture_output=True, text=True
            )
            rows.append(json.loads(proc.stdout))
            if n == configs[-1]:
                proc = subprocess.run(
                    [sys.executable, __file__, "run", str(d), "no-dedup"],
                    check=True, capture_output=True, text=True,
                )
                rows.append(json.loads(proc.stdout))
            shutil.rmtree(d)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{'shards':>8} {'records':>9} {'elapsed_s':>10} {'peak_rss_mb':>12} {'hash_ok':>8} {'dedup':>6}")
    for r in rows:
        print(
            f"{r['shards']:>8} {r['records']:>9} {r['elapsed_s']:>10} "
            f"{r['peak_rss_mb']:>12} {str(r['hash_ok']):>8} {str(r['detect_duplicates']):>6}"
        )
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "benchmark.json").write_text(json.dumps(rows, indent=2) + "\n")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "all":
        cmd_all()
    elif len(sys.argv) == 4 and sys.argv[1] == "gen":
        cmd_gen(Path(sys.argv[2]), int(sys.argv[3]))
    elif len(sys.argv) >= 3 and sys.argv[1] == "run":
        cmd_run(Path(sys.argv[2]), detect_duplicates=(len(sys.argv) < 4 or sys.argv[3] != "no-dedup"))
    else:
        print(__doc__)
        sys.exit(2)
