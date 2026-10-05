"""生成可复现的退避序列数据（data/backoff_sequence.{json,md}）。

用法: python3 tools/gen_sequence.py
种子固定为 20240601，任何机器上重跑输出逐字节一致。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from longpoll import Intent, ReconnectPolicy, Reconnector, SeedJitter, State, VirtualClock  # noqa: E402

SEED = 20240601
N = 12


def main():
    policy = ReconnectPolicy(max_failures=N + 1)  # base=1s, x2, cap=30s, timeout=25s
    clock = VirtualClock(1000.0)
    r = Reconnector(policy, clock, SeedJitter(SEED))
    r.start()

    rows = []
    for n in range(1, N + 1):
        clock.advance(policy.request_timeout)  # 请求超时
        assert r.on_timeout() is Intent.WAIT
        base = policy.base_backoff(n)
        rows.append({
            "failure": n,
            "base_s": base,
            "jitter": round(r.reconnect_at - clock.now(), 6) if base == 0 else None,
            "sleep_s": round(r.reconnect_at - clock.now(), 6),
            "reconnect_at": round(r.reconnect_at, 6),
            "state": r.state.value,
        })
        clock.advance(r.reconnect_at - clock.now())
        assert r.on_tick() is Intent.SEND

    # 补上每行的抖动样本值（sleep/base）
    for row in rows:
        row["jitter"] = round(row["sleep_s"] / row["base_s"], 6) if row["base_s"] else 0.0

    payload = {
        "seed": SEED,
        "policy": {
            "base_delay_s": policy.base_delay,
            "multiplier": policy.multiplier,
            "max_delay_s": policy.max_delay,
            "request_timeout_s": policy.request_timeout,
            "max_failures": policy.max_failures,  # 生成序列时放宽上界
            "max_server_delay_s": policy.max_server_delay,
        },
        "virtual_clock_start_s": 1000.0,
        "note": "sleep_s = jitter * min(base_delay*multiplier**(n-1), max_delay); "
                "reconnect_at 为虚拟时钟绝对时刻",
        "sequence": rows,
    }
    out_json = ROOT / "data" / "backoff_sequence.json"
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    lines = [
        "# 退避序列（seed=%d，可复现）" % SEED,
        "",
        "策略: base=1s, multiplier=2, cap=30s, request_timeout=25s, full jitter",
        "",
        "| 失败次数 n | base(n) s | 抖动样本 | sleep s | 重连时刻(虚拟时钟) |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append("| %d | %.1f | %.6f | %.6f | %.6f |" % (
            row["failure"], row["base_s"], row["jitter"],
            row["sleep_s"], row["reconnect_at"]))
    (ROOT / "data" / "backoff_sequence.md").write_text("\n".join(lines) + "\n")
    print("wrote", out_json, "and backoff_sequence.md")


if __name__ == "__main__":
    main()
