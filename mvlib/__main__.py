"""python3 -m mvlib <command> 入口。"""
import sys

USAGE = """用法:
  python3 -m mvlib check      对拍：增量刷新 vs 全量重算（含回退场景）
  python3 -m mvlib snapshot   快照一致性演示（刷新期间并发写入）
  python3 -m mvlib bench      性能对比：100 万行基线 + 10 万次变化
"""


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("check", "snapshot", "bench"):
        sys.stderr.write(USAGE)
        return 2
    cmd = sys.argv[1]
    if cmd == "check":
        from differential_check import main as run
    elif cmd == "snapshot":
        from snapshot_demo import main as run
    else:
        from bench import main as run
    return run()


if __name__ == "__main__":
    sys.exit(main())
