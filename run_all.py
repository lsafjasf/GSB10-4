"""Run the full delivery: self tests, fixtures, cross-check, errors, bench."""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV = dict(os.environ, PYTHONPATH=ROOT)

STEPS = [
    ("self tests (unittest)",
     [sys.executable, "-m", "unittest", "tests.test_sst", "-v"]),
    ("generate fixtures (data/)",
     [sys.executable, "tools/make_fixtures.py"]),
    ("cross-check compressed vs inline",
     [sys.executable, "tools/crosscheck.py"]),
    ("out-of-range / missing table report",
     [sys.executable, "tools/check_errors.py"]),
    ("benchmark 100k cells",
     [sys.executable, "tools/benchmark.py"]),
]


def main():
    for title, cmd in STEPS:
        print("\n### %s\n$ %s" % (title, " ".join(cmd)))
        proc = subprocess.run(cmd, cwd=ROOT, env=ENV)
        if proc.returncode != 0:
            print("STEP FAILED: %s" % title)
            return proc.returncode
    print("\nALL STEPS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
