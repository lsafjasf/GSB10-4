"""生成确定性退避序列数据 -> data/backoff_sequence.json（与 data/backoff_sequence.csv）。

同一 SEED、同一策略下输出逐位一致；测试里断言的前 12 个随机数即来源于此。
"""

import csv
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from longpoll.reconnect import BackoffPolicy  # noqa: E402

SEED = 20261005
POLICY = BackoffPolicy(request_timeout=30.0, base_delay=1.0, factor=2.0,
                       max_delay=60.0, max_attempts=12)


def main() -> None:
    rng = random.Random(SEED)
    rows = []
    t = 0.0  # 虚拟单调时钟（秒）
    # 每次完整周期：发起请求 -> 30s 超时 -> 退避 -> 到达重连时刻
    for n in range(1, POLICY.max_attempts + 1):
        t += POLICY.request_timeout
        failure_at = t
        raw = POLICY.raw_delay(n)
        capped = POLICY.capped_delay(n)
        u = rng.random()
        if n >= POLICY.max_attempts:
            rows.append({
                "failure": n, "failure_at": failure_at,
                "raw_delay": raw, "capped_delay": capped, "u": u,
                "delay_low": capped / 2, "delay_high": capped,
                "applied_delay": None, "reconnect_at": None,
                "state": "GIVEN_UP",
                "note": "达到连续失败上界，停止调度，仅 reset() 可恢复",
            })
            break
        delay = capped / 2.0 + u * capped / 2
        t += delay
        rows.append({
            "failure": n, "failure_at": failure_at,
            "raw_delay": raw, "capped_delay": capped, "u": u,
            "delay_low": capped / 2, "delay_high": capped,
            "applied_delay": delay, "reconnect_at": t,
            "state": "WAITING -> 到期 CONNECTING",
        })

    out = {
        "seed": SEED,
        "policy": {
            "request_timeout": POLICY.request_timeout,
            "base_delay": POLICY.base_delay,
            "factor": POLICY.factor,
            "max_delay": POLICY.max_delay,
            "max_attempts": POLICY.max_attempts,
        },
        "formula": {
            "raw": "base_delay * factor ** (n - 1)",
            "capped": "min(raw, max_delay)",
            "equal_jitter": "capped/2 + u*capped/2,  u = Random(seed).random()",
            "server_hint": "min(hint, max_delay)，优先于本地退避",
        },
        "schedule": rows,
    }
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    jpath = os.path.join(here, "data", "backoff_sequence.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    cpath = os.path.join(here, "data", "backoff_sequence.csv")
    fields = ["failure", "failure_at", "raw_delay", "capped_delay", "u",
              "delay_low", "delay_high", "applied_delay", "reconnect_at", "state"]
    with open(cpath, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {jpath} and {cpath}")


if __name__ == "__main__":
    main()
