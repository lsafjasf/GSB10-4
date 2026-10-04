"""Benchmark: validate a large theme and resolve 5000 icon queries.

Reports wall time, directory traversal (os.scandir) counts and
cached-vs-uncached consistency cross-check.
"""

import argparse
import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

from icon_theme.theme import DirCache, Resolver  # noqa: E402
from icon_theme.validate import validate_icons, validate_theme  # noqa: E402
import gen_fixture  # noqa: E402


def timed(fn):
    t0 = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - t0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--icons", type=int, default=5000)
    parser.add_argument("--keep", help="keep generated fixture in this dir")
    args = parser.parse_args(argv)

    if args.keep:
        os.makedirs(args.keep, exist_ok=True)
        base, cleanup = args.keep, None
    else:
        tmp = tempfile.TemporaryDirectory()
        base, cleanup = tmp.name, tmp.cleanup
    try:
        (icons, queries), t_build = timed(
            lambda: gen_fixture.build_bench_fixture(base, args.icons))
        theme = os.path.join(icons, "benchtheme")

        report, t_validate = timed(
            lambda: validate_theme(theme, cache=DirCache()))

        cache = DirCache()
        (results, summary), t_resolve = timed(
            lambda: validate_icons(theme, queries, cache=cache))

        def uncached():
            total_scans = 0
            fresh_results = []
            for query in queries:
                fresh_cache = DirCache()
                fresh_results.append(
                    Resolver(theme, cache=fresh_cache).resolve(*query))
                total_scans += fresh_cache.scandir_count
            return fresh_results, total_scans

        (fresh_results, scans_uncached), t_uncached = timed(uncached)

        mismatches = sum(
            1 for a, b in zip(results, fresh_results)
            if a.status != b.status
            or (a.hit is None) != (b.hit is None)
            or (a.hit and a.hit.path != b.hit.path)
            or len(a.corrupt) != len(b.corrupt)
        )

        print(f"icons queried: {args.icons}")
        print(f"fixture build: {t_build * 1000:.1f} ms")
        print()
        print(f"{'workload':<34} {'time (ms)':>10} {'dirs scanned':>14}")
        print("-" * 62)
        print(f"{'validate_theme (all files)':<34} "
              f"{t_validate * 1000:>10.1f} {report.dirs_scanned:>14}")
        print(f"{'resolve 5000 (shared cache)':<34} "
              f"{t_resolve * 1000:>10.1f} {summary['dirs_scanned']:>14}")
        print(f"{'resolve 5000 (fresh cache each)':<34} "
              f"{t_uncached * 1000:>10.1f} {scans_uncached:>14}")
        print()
        print("resolution summary:",
              f"hits={summary['hits']}",
              f"missing_clean={summary['missing_clean']}",
              f"missing_corrupt_only={summary['missing_corrupt_only']}",
              f"corrupt_files_seen={summary['corrupt_files_seen']}")
        print("corrupt files found by validate_theme:", len(report.corrupt))
        print("cached/uncached result mismatches:", mismatches)
        return 1 if mismatches else 0
    finally:
        if cleanup:
            cleanup()


if __name__ == "__main__":
    sys.exit(main())
