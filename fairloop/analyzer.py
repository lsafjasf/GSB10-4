"""事件循环公平性离线分析器。

输入：FairnessMonitor 记录的 RunRecord 序列（或任何同名结构）。
输出：确定性的 FairnessReport —— 同一份输入永远得到同一份输出，
分析过程不读取真实时钟、不使用随机数、不依赖字典遍历顺序。

度量口径（详见 docs/metrics.md）：
- 单次占用时长 duration = end_ts - start_ts
- 就绪延迟 delay      = start_ts - ready_ts
- 拖累归因（按占用分摊）：回调 B 在等待窗口 (ready_B, start_B] 内，
  凡与该窗口重叠的执行窗口 A 都按重叠时长计入 A 的“诱导等待”。
  即 B 的等待由“它等待期间实际占用循环的每个回调”按占用比例分摊；
  等待窗口内的空闲段不计入任何人。
"""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, asdict
import json


def _percentile(sorted_values, q):
    """最近秩分位数：输入必须已升序排序。确定性，无插值。"""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    rank = int(q * (n - 1) + 0.5)
    return sorted_values[rank]


def dist_stats(values):
    """分布统计：min/p50/p90/p99/max/mean —— 不只报平均值。"""
    if not values:
        return {"count": 0, "min": 0.0, "p50": 0.0, "p90": 0.0,
                "p99": 0.0, "max": 0.0, "mean": 0.0}
    ordered = sorted(values)
    n = len(ordered)
    return {
        "count": n,
        "min": ordered[0],
        "p50": _percentile(ordered, 0.5),
        "p90": _percentile(ordered, 0.9),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1],
        "mean": sum(ordered) / n,
    }


@dataclass(frozen=True)
class CulpritStat:
    """单个回调注册点 (name, site) 的拖累画像。"""
    name: str
    site: str
    first_reg_id: int           # 首次注册号（重注册会换新号）
    runs: int
    total_occupied: float       # 累计占用时长
    max_occupied: float         # 单次最大占用
    total_induced_wait: float   # 累计给他人造成的等待（按占用分摊）
    victims: int                # 被拖累的回调执行次数
    score: float                # 排序分 = total_induced_wait


@dataclass(frozen=True)
class FairnessReport:
    total_runs: int
    wall_time: float            # 末次结束 - 首次开始
    busy_time: float            # 各执行窗口并集长度
    idle_time: float            # wall - busy（空闲时间片）
    utilization: float
    duration: dict              # 单次占用时长分布
    delay: dict                 # 就绪延迟分布
    worst_offender: tuple       # 单次执行诱导等待之最 (induced, name, site) 或 None
    culprits: tuple             # 按拖累排序的 CulpritStat
    stalled_runs: int           # delay > stall_threshold 的执行次数

    def to_dict(self):
        data = asdict(self)
        if self.worst_offender is not None:
            data["worst_offender"] = list(self.worst_offender)
        data["culprits"] = [asdict(c) for c in self.culprits]
        return data

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def _validate(records):
    for rec in records:
        if rec.end_ts < rec.start_ts:
            raise ValueError(f"negative duration in record {rec!r}")
        if rec.start_ts < rec.ready_ts:
            raise ValueError(f"run started before it was ready: {rec!r}")


def _merge_busy(runs):
    """执行窗口并集长度（runs 已按 start 排序）。"""
    busy = 0.0
    cur_start = None
    cur_end = None
    for rec in runs:
        if cur_start is None:
            cur_start, cur_end = rec.start_ts, rec.end_ts
        elif rec.start_ts <= cur_end:
            if rec.end_ts > cur_end:
                cur_end = rec.end_ts
        else:
            busy += cur_end - cur_start
            cur_start, cur_end = rec.start_ts, rec.end_ts
    if cur_start is not None:
        busy += cur_end - cur_start
    return busy


def _attribute(runs):
    """按占用分摊的拖累归因。

    返回 (induced, victims)：induced[i] 为第 i 个执行窗口给他人造成的
    总等待时长，victims[i] 为它拖累的执行次数。
    复杂度 O(n log n)：等待事件扫描线 + 差分数组。
    """
    n = len(runs)
    induced = [0.0] * n
    victims = [0] * n
    starts = [rec.start_ts for rec in runs]
    ends = [rec.end_ts for rec in runs]

    waits = []  # (ready, start) 仅保留正延迟
    for rec in runs:
        if rec.start_ts > rec.ready_ts:
            waits.append((rec.ready_ts, rec.start_ts))
    if not waits:
        return induced, victims

    # ---- 扫描线：induced[i] = ∫_{窗口i} 活跃等待数 dt ----
    events = {}
    for ready, start in waits:
        events[ready] = events.get(ready, 0) + 1
        events[start] = events.get(start, 0) - 1
    points = sorted(events)
    lo = 0  # 第一个 end > 当前段起点 的窗口下标（单调推进）
    active = 0
    prev = None
    for point in points:
        if prev is not None and point > prev and active > 0:
            # 段 [prev, point) 内有 active 个等待正在进行
            while lo < n and ends[lo] <= prev:
                lo += 1
            k = lo
            while k < n and starts[k] < point:
                overlap = min(ends[k], point) - max(starts[k], prev)
                if overlap > 0:
                    induced[k] += active * overlap
                k += 1
        active += events[point]
        prev = point

    # ---- 差分数组：每个等待给它覆盖的连续窗口区间 +1 ----
    diff = [0] * (n + 1)
    for ready, start in waits:
        a = bisect_right(ends, ready)      # 第一个 end > ready
        b = bisect_left(starts, start) - 1  # 最后一个 start < start
        if 0 <= a <= b < n:
            diff[a] += 1
            diff[b + 1] -= 1
    cur = 0
    for i in range(n):
        cur += diff[i]
        # 零时长窗口不可能拖累任何人
        victims[i] = cur if ends[i] > starts[i] else 0
    return induced, victims


def analyze(records, stall_threshold=0.0):
    """对一段运行记录做公平性分析。纯函数，确定性。

    stall_threshold: 就绪延迟超过该值记为一次“被卡住”的执行。
    """
    records = list(records)
    _validate(records)
    if not records:
        return FairnessReport(
            total_runs=0, wall_time=0.0, busy_time=0.0, idle_time=0.0,
            utilization=0.0, duration=dist_stats([]), delay=dist_stats([]),
            worst_offender=None, culprits=(), stalled_runs=0,
        )

    # 确定性排序：与输入顺序无关，完全由记录内容决定。
    runs = sorted(records, key=lambda r: (
        r.start_ts, r.end_ts, r.ready_ts, r.reg_id, r.name, r.site))

    durations = [rec.end_ts - rec.start_ts for rec in runs]
    delays = [rec.start_ts - rec.ready_ts for rec in runs]

    induced, victims = _attribute(runs)

    worst = None  # (induced, -reg_id, name, site)
    for i, rec in enumerate(runs):
        if induced[i] > 0 and (worst is None or induced[i] > worst[0]):
            worst = (induced[i], rec.name, rec.site)

    # ---- 按注册点 (name, site) 聚合（重注册共享同一身份）----
    agg = {}
    order = []
    for i, rec in enumerate(runs):
        key = (rec.name, rec.site)
        entry = agg.get(key)
        if entry is None:
            entry = agg[key] = {
                "first_reg_id": rec.reg_id, "runs": 0,
                "total_occupied": 0.0, "max_occupied": 0.0,
                "total_induced": 0.0, "victims": 0,
            }
            order.append(key)
        dur = rec.end_ts - rec.start_ts
        entry["runs"] += 1
        entry["total_occupied"] += dur
        if dur > entry["max_occupied"]:
            entry["max_occupied"] = dur
        entry["total_induced"] += induced[i]
        entry["victims"] += victims[i]

    culprits = [CulpritStat(
        name=key[0], site=key[1],
        first_reg_id=agg[key]["first_reg_id"], runs=agg[key]["runs"],
        total_occupied=agg[key]["total_occupied"],
        max_occupied=agg[key]["max_occupied"],
        total_induced_wait=agg[key]["total_induced"],
        victims=agg[key]["victims"],
        score=agg[key]["total_induced"],
    ) for key in order]
    # 排序：累计拖累降序 -> 单次最大占用降序 -> (name, site) 升序（确定性）
    culprits.sort(key=lambda c: (-c.score, -c.max_occupied, c.name, c.site))

    first_start = runs[0].start_ts
    last_end = max(rec.end_ts for rec in runs)
    wall = last_end - first_start
    busy = _merge_busy(runs)
    idle = wall - busy
    stalled = sum(1 for d in delays if d > stall_threshold)

    return FairnessReport(
        total_runs=len(runs),
        wall_time=wall,
        busy_time=busy,
        idle_time=idle,
        utilization=(busy / wall) if wall > 0 else 0.0,
        duration=dist_stats(durations),
        delay=dist_stats(delays),
        worst_offender=worst,
        culprits=tuple(culprits),
        stalled_runs=stalled,
    )
