"""生成可复现的判定序列数据 sequences.json。

四个场景（idle=30, interval=5, max_probes=3）：
  1. data_traffic  有数据往来（入站间隔 29s）
  2. pure_idle      纯空闲
  3. half_open_tx   单向半开（只 TX 不 RX）
  4. threshold_tie  探测正好卡在阈值 + 应答卡判死时刻

每个场景同时输出引擎状态序列与独立参考模型序列，二者必须逐点一致。
运行: python3 gen_sequences.py
"""

import json

from keepalive import Event, KeepaliveEngine, State, simulate
from test_keepalive import reference

ENG = KeepaliveEngine(30.0, 5.0, 3)


def script_data_traffic():
    # 每 29s 一个 RX_DATA（始终 < idle_timeout 30s），TX 穿插其间不重置
    sc = []
    for t in range(0, 121):
        if t % 29 == 0 and t > 0:
            sc.append((float(t), Event.RX_DATA))
        elif t % 2:
            sc.append((float(t), Event.TX_DATA))
        else:
            sc.append((float(t), None))
    return sc


def script_pure_idle():
    return [(float(t), None) for t in range(46)]


def script_half_open():
    # 对端 t=0 后静默消失；本端每秒 TX_DATA
    return [(float(t), Event.TX_DATA if t else None) for t in range(46)]


def script_threshold():
    # 0..44 纯静默 -> 已发满 3 探测；t=45 应答与判死同刻到达（事件优先 -> 存活）
    sc = [(float(t), None) for t in range(45)]
    sc.append((45.0, Event.RX_PROBE_ACK))
    return sc


SCENARIOS = {
    "data_traffic": script_data_traffic,
    "pure_idle": script_pure_idle,
    "half_open_tx": script_half_open,
    "threshold_tie": script_threshold,
}


def serialize(script, trace):
    rows = []
    for (t, event), (_, state, probes) in zip(script, trace):
        rows.append({
            "t": t,
            "event": event.name if event is not None else "-",
            "state": state.name,
            "probes_sent": probes,
        })
    return rows


def main():
    out = {
        "config": {"idle_timeout": 30.0, "probe_interval": 5.0,
                    "max_probes": 3, "dead_after_seconds": 45.0},
        "scenarios": {},
    }
    for name, factory in SCENARIOS.items():
        script = factory()
        trace_eng = simulate(ENG, script)
        trace_ref = reference(script)
        assert trace_eng == trace_ref, f"{name}: 引擎与参考模型不一致"
        rows = serialize(script, trace_eng)
        out["scenarios"][name] = {
            "matches_reference": True,
            "len": len(rows),
            "sequence": rows,
        }
    with open("sequences.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("wrote sequences.json")
    for name, sc in out["scenarios"].items():
        seq = sc["sequence"]
        final = seq[-1]
        print(f"  {name:14s} steps={sc['len']:3d} final={final['state']:11s} "
              f"probes={final['probes_sent']}")


if __name__ == "__main__":
    main()
