#!/usr/bin/env python3
"""selftest.py -- 自测：覆盖全健康 / 单后端摘除 / 全部不可用 / 后端恢复。

产出（reports/ 下）：
  scenarios.txt            各场景断言明细
  state_diff.txt           迁移前后状态对拍数据
  ownership_trajectory.csv 恢复序列下的会话归属轨迹
退出码非 0 表示有断言失败。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sticky import (MIGRATE, REJECT, StaleEpochError, StickyCoordinator,
                    SessionState, _checksum)

REPORTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
FAILURES = []
SCEN_LOG = []
DIFF_LOG = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}" + (f"  -- {detail}" if detail else "")
    SCEN_LOG.append(line)
    if not cond:
        FAILURES.append(line)


def sid(i):
    return f"sess-{i:05d}"


def seed_sessions(coord, n):
    """绑定 n 个会话并写入确定性状态，返回 {sid: checksum}。"""
    expected = {}
    for i in range(n):
        s = sid(i)
        sel = coord.bind(s)
        assert sel.backend, f"bind failed for {s}"
        def mut(payload, version, i=i):
            payload["seq"] = i
            payload["version"] = version
            payload["blob"] = f"payload-{i:05d}-" + "x" * (i % 64)
        coord.write_session(s, mut)
        coord.write_session(s, mut)  # 写两次，version=2
        expected[s] = _checksum(coord.read_state(s))
    return expected


def diff_report(title, coord, expected, sessions):
    """对拍：期望 checksum vs 当前归属后端上的实际 checksum。"""
    DIFF_LOG.append(f"== {title} ==")
    DIFF_LOG.append("session,expected_checksum,actual_checksum,owner,match")
    ok = True
    for s in sessions:
        owner = coord.pin_of(s)
        st = coord.backends[owner].get_state(s) if owner else None
        actual = st.checksum if st else "<missing>"
        match = expected[s] == actual
        ok = ok and match
        DIFF_LOG.append(f"{s},{expected[s]},{actual},{owner},{match}")
    DIFF_LOG.append(f"summary: {len(sessions)} sessions, all_match={ok}")
    DIFF_LOG.append("")
    return ok


# ---------------------------------------------------------------- 场景 1：全健康
def scenario_all_healthy():
    SCEN_LOG.append("== 场景1：后端全健康 ==")
    coord = StickyCoordinator(["b1", "b2", "b3", "b4"], policy=MIGRATE, capacity=1500)
    n = 4000
    expected = seed_sessions(coord, n)

    first = {sid(i): coord.pin_of(sid(i)) for i in range(n)}
    for i in range(n):  # 重复选择：粘性保持
        sel = coord.bind(sid(i))
        if sel.backend != first[sid(i)] or sel.outcome != "ok_pinned":
            check("粘性保持", False, f"{sid(i)} -> {sel.backend}")
            break
    else:
        check("粘性保持：4000 会话重复选择归属不变", True)

    loads = coord.loads()
    check("负载上界：每后端 <= capacity=1500", all(v <= 1500 for v in loads.values()),
          str(loads))
    check("负载下限：每后端 >= 900（有界负载）", all(v >= 900 for v in loads.values()),
          str(loads))
    check("会话总数守恒", sum(loads.values()) == n, str(sum(loads.values())))

    coord2 = StickyCoordinator(["b1", "b2", "b3", "b4"], policy=MIGRATE, capacity=1500)
    for i in range(n):
        coord2.bind(sid(i))
    same = all(coord2.pin_of(sid(i)) == first[sid(i)] for i in range(n))
    check("冷启动确定性：重放同样序列，归属完全一致", same)

    check("状态对拍（全健康）", diff_report("全健康基线", coord, expected,
                                            [sid(i) for i in range(n)]))
    return coord, expected


# ------------------------------------------------- 场景 2：单后端摘除（优雅排空）
def scenario_single_removal(coord, expected):
    SCEN_LOG.append("")
    SCEN_LOG.append("== 场景2：单后端摘除（drain, policy=migrate） ==")
    victim_sessions = sorted(s for s in coord.pins if coord.pin_of(s) == "b2")
    before_map = {s: coord.pin_of(s) for s in coord.pins}
    stats = coord.remove_backend("b2", graceful=True)
    SCEN_LOG.append(f"摘除 b2：{stats}")

    check("迁移数量 = 原 b2 会话数", stats["migrated"] == len(victim_sessions),
          f"{stats['migrated']} vs {len(victim_sessions)}")
    check("迁移零丢失", stats["lost"] == 0 and all(m.ok for m in coord.migrations))
    moved = [s for s in coord.pins if coord.pin_of(s) != before_map[s]]
    check("最小迁移：只有被摘除后端上的会话移动",
          sorted(moved) == victim_sessions, f"moved={len(moved)}")
    check("迁移目标均健康", all(coord.backends[coord.pin_of(s)].healthy for s in victim_sessions))

    # 漂移后粘性：再次选择结果确定且不变
    after_map = {s: coord.pin_of(s) for s in coord.pins}
    drift_ok = all(coord.bind(s).backend == after_map[s] for s in list(coord.pins)[:2000])
    check("漂移后粘性：2000 会话重新选择归属不变", drift_ok)

    # fencing：旧后端迟到的写必须被拒
    s0 = victim_sessions[0]
    old_epoch = 1
    try:
        coord.write_on_backend("b2", s0, {"stale": True}, old_epoch)
        check("fencing：旧 epoch 写入被拒", False)
    except StaleEpochError:
        check("fencing：旧 epoch 写入被拒", True)

    check("状态对拍（drain 迁移后）",
          diff_report("drain 迁移后", coord, expected, sorted(coord.pins)))
    return coord, expected, victim_sessions


# ------------------------------------------------- 场景 3：单后端摘除（崩溃）
def scenario_crash_removal():
    SCEN_LOG.append("")
    SCEN_LOG.append("== 场景3：单后端摘除（crash, policy=migrate, WAL 恢复） ==")
    coord = StickyCoordinator(["b1", "b2", "b3", "b4"], policy=MIGRATE, capacity=1500)
    expected = seed_sessions(coord, 4000)
    victim = sorted(s for s in coord.pins if coord.pin_of(s) == "b3")
    stats = coord.remove_backend("b3", graceful=False)  # 崩溃：内存态丢失
    SCEN_LOG.append(f"崩溃 b3：{stats}")
    check("崩溃迁移数量正确", stats["migrated"] == len(victim))
    check("崩溃迁移零丢失（WAL 恢复）",
          stats["lost"] == 0 and all(m.ok for m in coord.migrations))
    check("状态对拍（crash 迁移后）",
          diff_report("crash 迁移后", coord, expected, sorted(coord.pins)))
    return coord, expected


# ------------------------------------------------- 场景 4：REJECT 策略
def scenario_reject_policy():
    SCEN_LOG.append("")
    SCEN_LOG.append("== 场景4：单后端摘除（policy=reject） ==")
    coord = StickyCoordinator(["b1", "b2", "b3"], policy=REJECT, capacity=2000)
    expected = seed_sessions(coord, 3000)
    victim = sorted(s for s in coord.pins if coord.pin_of(s) == "b2")
    stats = coord.remove_backend("b2", graceful=True)
    SCEN_LOG.append(f"摘除 b2：{stats}")
    check("REJECT：无迁移", stats["migrated"] == 0 and len(coord.migrations) == 0)
    check("REJECT：受影响会话全部被拒", stats["rejected"] == len(victim))
    rej = all(coord.bind(s).outcome == "rejected" for s in victim[:500])
    check("REJECT：选择返回 rejected", rej)
    alive = [s for s in coord.pins if coord.pins[s].backend != "b2"]
    ok_alive = all(coord.bind(s).outcome == "ok_pinned" for s in alive[:500])
    check("REJECT：其他后端会话不受影响", ok_alive)
    # 状态保留在 b2 的 WAL 中
    kept = all(coord.backends["b2"].journal.get(s) is not None and
               coord.backends["b2"].journal[s].checksum == expected[s]
               for s in victim)
    check("REJECT：状态完整保留在原后端 WAL", kept)
    return coord, expected, victim


# ------------------------------------------------- 场景 5：全部不可用
def scenario_all_down():
    SCEN_LOG.append("")
    SCEN_LOG.append("== 场景5：后端全部不可用 ==")
    coord = StickyCoordinator(["b1", "b2"], policy=MIGRATE, capacity=2000)
    expected = seed_sessions(coord, 2000)
    coord.remove_backend("b1", graceful=True)
    stats = coord.remove_backend("b2", graceful=True)
    SCEN_LOG.append(f"摘除 b2（最后一个）：{stats}")
    check("全不可用：迁移失败计数正确", stats["failed"] == 2000)
    outcomes = {coord.bind(sid(i)).outcome for i in range(200)}
    check("全不可用：选择返回 no_backend", outcomes == {"no_backend"}, str(outcomes))
    check("全不可用：无状态丢失",
          diff_report("全部不可用（状态留在最后归属后端）", coord, expected,
                      [sid(i) for i in range(2000)]))
    return coord, expected


# ------------------------------------------------- 场景 6：恢复序列归属轨迹
TRAJ_STEPS = ["初始", "摘除b2", "恢复b2", "摘除b1", "恢复b1", "摘除b3", "恢复b3"]


def trajectory_world(policy, crash=False):
    coord = StickyCoordinator(["b1", "b2", "b3"], policy=policy, capacity=2000)
    n = 30
    sessions = [sid(i) for i in range(n)]
    for s in sessions:
        coord.bind(s)
        coord.write_session(s, lambda p, v: p.update(v=v))

    rows = {s: [coord.pin_of(s) or ("REJECTED" if coord.pins[s].status == "rejected" else "NONE")]
            for s in sessions}
    coord.remove_backend("b2", graceful=not crash)
    for s in sessions:
        sel = coord.bind(s)
        rows[s].append(sel.backend or sel.outcome.upper())
    coord.recover_backend("b2")
    for s in sessions:
        sel = coord.bind(s)
        rows[s].append(sel.backend or sel.outcome.upper())
    coord.remove_backend("b1", graceful=True)
    for s in sessions:
        sel = coord.bind(s)
        rows[s].append(sel.backend or sel.outcome.upper())
    coord.recover_backend("b1")
    for s in sessions:
        sel = coord.bind(s)
        rows[s].append(sel.backend or sel.outcome.upper())
    coord.remove_backend("b3", graceful=True)
    for s in sessions:
        sel = coord.bind(s)
        rows[s].append(sel.backend or sel.outcome.upper())
    coord.recover_backend("b3")
    for s in sessions:
        sel = coord.bind(s)
        rows[s].append(sel.backend or sel.outcome.upper())
    return coord, rows


def scenario_recovery_trajectory():
    SCEN_LOG.append("")
    SCEN_LOG.append("== 场景6：后端恢复后的归属轨迹（不抢回） ==")
    csv_lines = ["session,step," + ",".join(TRAJ_STEPS)]

    coord, rows = trajectory_world(MIGRATE)
    for s in sessions_sorted(rows):
        csv_lines.append(f"{s},migrate," + ",".join(rows[s]))
    # 断言：恢复 b2 后，原本从 b2 迁走的会话不归 b2
    for s, traj in rows.items():
        if traj[0] == "b2" and traj[1] != "b2":
            check(f"不抢回(migrate) {s}", traj[2] == traj[1],
                  f"恢复b2后={traj[2]} 期望={traj[1]}")
    migrated_off_b2 = [s for s, t in rows.items() if t[0] == "b2" and t[1] != "b2"]
    check("恢复后无抢回（migrate 汇总）",
          all(rows[s][2] == rows[s][1] for s in migrated_off_b2),
          f"{len(migrated_off_b2)} 个迁出会话全部留在新后端")
    # 最终所有会话健康且状态在
    final_ok = all(coord.bind(s).outcome == "ok_pinned" for s in rows)
    check("轨迹结束：全部会话 ok_pinned", final_ok)

    coord_r, rows_r = trajectory_world(REJECT)
    for s in sessions_sorted(rows_r):
        csv_lines.append(f"{s},reject," + ",".join(rows_r[s]))
    # REJECT：恢复后归还原主
    back_home = all(t[2] == "b2" for s, t in rows_r.items() if t[0] == "b2")
    check("REJECT：恢复后会话归还原后端", back_home)

    # 全部摘除再恢复
    coord_d = StickyCoordinator(["b1", "b2", "b3"], policy=MIGRATE, capacity=2000)
    for i in range(30):
        coord_d.bind(sid(i))
        coord_d.write_session(sid(i), lambda p, v: p.update(v=v))
    exp = {sid(i): coord_d.backends[coord_d.pin_of(sid(i))].get_state(sid(i)).checksum
           for i in range(30)}
    for b in ("b1", "b2", "b3"):
        coord_d.remove_backend(b, graceful=True)
    down = {coord_d.bind(sid(i)).outcome for i in range(30)}
    check("全摘除：全部 no_backend", down == {"no_backend"})
    for b in ("b1", "b2", "b3"):
        coord_d.recover_backend(b)
    stayed = all(coord_d.bind(sid(i)).backend == coord_d.pin_of(sid(i)) for i in range(30))
    check("全摘除恢复：归属不变（不抢回）", stayed)
    integ = all(coord_d.backends[coord_d.pin_of(sid(i))].get_state(sid(i)).checksum == exp[sid(i)]
                for i in range(30))
    check("全摘除恢复：状态完整", integ)
    for i in range(30):
        s = sid(i)
        traj = [s, "all_down"]
        csv_lines.append(f"{s},all_down," + ",".join([coord_d.pin_of(s)] * 7))

    with open(os.path.join(REPORTS, "ownership_trajectory.csv"), "w") as f:
        f.write("\n".join(csv_lines) + "\n")
    SCEN_LOG.append(f"归属轨迹已写入 reports/ownership_trajectory.csv "
                    f"（{len(csv_lines) - 1} 行）")


def sessions_sorted(rows):
    return sorted(rows.keys())


def main():
    os.makedirs(REPORTS, exist_ok=True)
    coord1, exp1 = scenario_all_healthy()
    scenario_single_removal(coord1, exp1)
    scenario_crash_removal()
    scenario_reject_policy()
    scenario_all_down()
    scenario_recovery_trajectory()

    with open(os.path.join(REPORTS, "scenarios.txt"), "w") as f:
        f.write("\n".join(SCEN_LOG) + "\n")
    with open(os.path.join(REPORTS, "state_diff.txt"), "w") as f:
        f.write("\n".join(DIFF_LOG) + "\n")

    print("\n".join(SCEN_LOG))
    print(f"\n对拍数据 -> reports/state_diff.txt ({len(DIFF_LOG)} 行)")
    print(f"归属轨迹 -> reports/ownership_trajectory.csv")
    if FAILURES:
        print(f"\n{len(FAILURES)} 项失败：")
        print("\n".join(FAILURES))
        sys.exit(1)
    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
