"""在不同位点制造 SIGKILL 崩溃点，采集恢复结果到 crash_results.json。"""

import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
N = 2000


def start(workdir, sleep):
    env = dict(os.environ, CHILD_SLEEP_EACH=sleep)
    return subprocess.Popen(
        [sys.executable, os.path.join(HERE, "crash_child.py"),
         os.path.join(workdir, "wal.log"),
         os.path.join(workdir, "processed.log"), str(N)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def trial(seed):
    rng = random.Random(seed)
    with tempfile.TemporaryDirectory() as d:
        p = start(d, "0.001")
        time.sleep(rng.uniform(0.05, 0.5))  # 让子进程先跑一小段
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        p.stdout.close(); p.stderr.close()

        processed_at_crash = [int(x) for x in
                              open(os.path.join(d, "processed.log"))]
        crash_after = max(processed_at_crash) if processed_at_crash else -1

        p2 = start(d, "0")
        out, err = p2.communicate(timeout=60)
        assert p2.returncode == 0, err
        processed = [int(x) for x in open(os.path.join(d, "processed.log"))]

        return {
            "seed": seed,
            "crash_after_offset": crash_after,
            "committed_base_on_recovery": int(out.split("start=")[1].split()[0]),
            "processed_count_crash": len(processed_at_crash),
            "processed_count_final": len(processed),
            "duplicate_replays": len(processed) - len(set(processed)),
            "skipped": sorted(set(range(N)) - set(processed)),
            "child_restart_output": out.strip(),
        }


if __name__ == "__main__":
    rows = [trial(seed) for seed in (1, 2, 3, 4, 5)]
    out = os.path.join(HERE, "crash_results.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"messages": N, "rule": "先处理后写D(fsync); 恢复从已提交base续跑",
                   "trials": rows}, fh, ensure_ascii=False, indent=2)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
