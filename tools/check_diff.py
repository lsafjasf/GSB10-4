"""对拍：把库的解析结果与 data/diff_cases.json 中手工核对的预期逐条比对。"""

import argparse
import json
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

from icon_theme.theme import DirCache, Resolver  # noqa: E402
import gen_fixture  # noqa: E402


def run_case(resolver, case):
    q = case["query"]
    res = resolver.resolve(q["name"], q["size"], q["context"])
    exp = case["expect"]
    errors = []
    if res.status != exp["status"]:
        errors.append(f"status {res.status} != {exp['status']}")
    if len(res.corrupt) != exp["corrupt"]:
        errors.append(f"corrupt count {len(res.corrupt)} != {exp['corrupt']}")
    if exp["status"] == "hit":
        hit = res.hit
        if hit.stage != exp["stage"]:
            errors.append(f"stage {hit.stage} != {exp['stage']}")
        if hit.level != exp["level"]:
            errors.append(f"level {hit.level} != {exp['level']}")
        if hit.level_name != exp["level_name"]:
            errors.append(f"level_name {hit.level_name} != {exp['level_name']}")
        posix_path = hit.path.replace(os.sep, "/")
        if not posix_path.endswith(exp["path_suffix"]):
            errors.append(f"path ...{posix_path[-60:]} "
                          f"does not end with {exp['path_suffix']}")
    return res, errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", help="keep generated fixture in this dir")
    args = parser.parse_args(argv)

    with open(os.path.join(REPO_ROOT, "data", "diff_cases.json"),
              encoding="utf-8") as fh:
        data = json.load(fh)

    if args.keep:
        base = args.keep
        os.makedirs(base, exist_ok=True)
        cleanup = None
    else:
        tmp = tempfile.TemporaryDirectory()
        base = tmp.name
        cleanup = tmp.cleanup
    try:
        icons = gen_fixture.build_diff_fixture(base)
        cache = DirCache()
        resolver = Resolver(os.path.join(icons, data["theme"]), cache=cache)
        failed = 0
        print(f"{'id':<46} {'result':<7} hit/corrupt  detail")
        print("-" * 100)
        for case in data["cases"]:
            res, errors = run_case(resolver, case)
            ok = not errors
            failed += not ok
            detail = "OK" if ok else "; ".join(errors)
            where = res.hit.describe() if res.hit else "-"
            print(f"{case['id']:<46} {'PASS' if ok else 'FAIL':<7} "
                  f"{len(res.corrupt)} corrupt     {where}")
            if not ok:
                print(f"    mismatch: {detail}")
        print("-" * 100)
        print(f"{len(data['cases']) - failed}/{len(data['cases'])} cases pass, "
              f"{failed} fail, dirs scanned={cache.scandir_count}")
        return 1 if failed else 0
    finally:
        if cleanup:
            cleanup()


if __name__ == "__main__":
    sys.exit(main())
