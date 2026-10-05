"""物化视图：定义、全量构建、增量刷新、回退判定。

支持的视图形态（规则见 RULES.md）：

    SELECT <group_col>, COUNT(*), SUM(<sum_col>), MIN(<sum_col>), MAX(<sum_col>)
    FROM <table> GROUP BY <group_col>

- 增量可维护：COUNT / SUM 用加减法；MIN / MAX 用「堆 + 懒删除计数器」，
  删除值不立即出堆，只在读结果时把已失效的堆顶弹掉。
- 增量不可维护（识别后退回全量，绝不硬算）：
    1. 表被 TRUNCATE（清空）；
    2. 表发生 schema 变更（列增删改）；
    3. changelog 出现缺口（事务号不连续，说明有变化丢失）；
    4. 视图定义本身变化（group/sum 列、表名等被改动）；
    5. 成本判断：本次待处理变化数 > 快照存活行数 × 阈值，增量不划算。
"""

import heapq
from collections import Counter

from .storage import BOUNDARY, TRUNCATE_MARK, SCHEMA_MARK

# 成本回退阈值：变化数超过存活行数 × 该比例时退回全量
COST_RATIO = 0.6

# 增量可维护的聚合函数（列 -> 维护方式）
_SUPPORTED_AGGREGATES = ("count", "sum", "min", "max")


class RefreshReport:
    """一次刷新的结果报告。"""

    def __init__(self, mode, snapshot, changes, applied, reason, rows):
        self.mode = mode          # "incremental" | "full" | "none"
        self.snapshot = snapshot  # 本次刷新读到的一致性快照
        self.changes = changes    # 待处理变化条数
        self.applied = applied    # 实际增量应用条数（全量时为 0）
        self.reason = reason      # 退回全量的原因；增量成功为 None
        self.rows = rows          # 刷新后视图行数

    def as_dict(self):
        return {
            "mode": self.mode,
            "snapshot_xid": self.snapshot.xid if self.snapshot else None,
            "changes": self.changes,
            "applied": self.applied,
            "fallback_reason": self.reason,
            "rows": self.rows,
        }


class _GroupState:
    """单个分组键的聚合状态。"""

    __slots__ = ("count", "total", "min_heap", "min_ctr", "max_heap", "max_ctr")

    def __init__(self):
        self.count = 0
        self.total = 0
        self.min_heap = []   # 存值
        self.min_ctr = Counter()
        self.max_heap = []   # 存负值
        self.max_ctr = Counter()


class MaterializedView:
    def __init__(self, name, table, group_col, sum_col, cost_ratio=COST_RATIO):
        for agg in _SUPPORTED_AGGREGATES:
            pass  # 聚合集合固定，无需校验
        self.name = name
        self.table = table
        self.group_col = group_col
        self.sum_col = sum_col
        self.cost_ratio = cost_ratio
        self._groups = {}          # group_key -> _GroupState
        self._last_xid = None      # 视图已同步到的最新事务号
        self._lock = None          # 视图读用快照，无需锁

    # ---------- 定义变更（触发回退规则 4） ----------

    def redefine(self, group_col=None, sum_col=None):
        """修改视图定义。定义变化无法增量处理，标记为需要全量重建。"""
        if group_col is not None:
            self.group_col = group_col
        if sum_col is not None:
            self.sum_col = sum_col
        self._last_xid = None      # 定义失效：下次 refresh 必须全量

    # ---------- 全量构建 ----------

    def _full_build(self, snapshot):
        groups = {}
        for _pk, row in self.table.iter_snapshot(snapshot):
            gk = row[self.group_col]
            st = groups.get(gk)
            if st is None:
                st = groups[gk] = _GroupState()
            v = row[self.sum_col]
            st.count += 1
            st.total += v
            st.min_ctr[v] += 1
            st.max_ctr[v] += 1
        for st in groups.values():
            st.min_heap = list(st.min_ctr)
            heapq.heapify(st.min_heap)
            st.max_heap = [-v for v in st.max_ctr]
            heapq.heapify(st.max_heap)
        self._groups = groups
        self._last_xid = snapshot.xmax_snap

    # ---------- 增量应用 ----------

    def _apply_insert(self, row):
        gk = row[self.group_col]
        st = self._groups.get(gk)
        if st is None:
            st = self._groups[gk] = _GroupState()
        v = row[self.sum_col]
        st.count += 1
        st.total += v
        heapq.heappush(st.min_heap, v)
        st.min_ctr[v] += 1
        heapq.heappush(st.max_heap, -v)
        st.max_ctr[v] += 1

    def _apply_delete(self, row):
        gk = row[self.group_col]
        st = self._groups.get(gk)
        if st is None:
            # 视图里没有该组却收到删除：状态与日志不一致，不能硬算
            raise InconsistentState("删除的分组 %r 在视图中不存在" % (gk,))
        v = row[self.sum_col]
        st.count -= 1
        st.total -= v
        st.min_ctr[v] -= 1
        if st.min_ctr[v] == 0:
            del st.min_ctr[v]
        st.max_ctr[v] -= 1
        if st.max_ctr[v] == 0:
            del st.max_ctr[v]
        if st.count == 0:
            # 组内行数归零：整组从视图中移除（与全量重算的 GROUP BY 行为一致）
            del self._groups[gk]

    def _apply_entry(self, entry):
        _xid, op, pk, before, after = entry
        if op == "INSERT":
            self._apply_insert(after)
        elif op == "DELETE":
            self._apply_delete(before)
        elif op == "UPDATE":
            self._apply_delete(before)
            self._apply_insert(after)
        else:
            raise ValueError("未知日志操作: %r" % (op,))

    # ---------- 回退判定 ----------

    def _check_fallback(self, entries, snapshot):
        """返回 (需要全量的原因 or None, 逐行条目数)。"""
        row_changes = 0
        prev_xid = self._last_xid
        for entry in entries:
            xid, op, pk, _before, _after = entry
            if op == "MARK":
                if pk in (TRUNCATE_MARK, SCHEMA_MARK):
                    return "表结构级变化(%s)，无法按行增量" % pk, row_changes
                # BOUNDARY 是基线标记，出现在 last_xid 之前，正常不会走到
                continue
            if xid != prev_xid + 1:
                return ("changelog 缺口：期望事务号 %d，实际 %d，"
                        "存在丢失的变化" % (prev_xid + 1, xid)), row_changes
            prev_xid = xid
            row_changes += 1
        if prev_xid != snapshot.xmax_snap:
            return ("changelog 缺口：日志止于事务号 %d，快照高水位为 %d，"
                    "存在丢失的变化" % (prev_xid, snapshot.xmax_snap)), row_changes
        live = self.table.live_count()
        if row_changes > live * self.cost_ratio:
            return ("成本判断：%d 条变化 > 存活行数 %d × %.2f，增量不划算"
                    % (row_changes, live, self.cost_ratio)), row_changes
        return None, row_changes

    # ---------- 刷新 ----------

    def refresh(self, during_snapshot=None):
        """刷新视图。返回 RefreshReport。

        during_snapshot: 可选回调，在快照建立之后、应用变化之前调用，
        用于演示/验证「刷新期间读到的是一致快照」。
        """
        snapshot = self.table.snapshot()
        entries, _tail = self.table.log_tail(self._last_xid)
        if self._last_xid is None:
            # 从未构建（或定义被改）：全量
            self._full_build(snapshot)
            if during_snapshot:
                during_snapshot(snapshot)
            return RefreshReport("full", snapshot, 0, 0, "首次构建/定义变更",
                                 len(self._groups))
        # 只取 (last_xid, 快照高水位] 区间内的条目；
        # 快照建立之后才提交的变更留给下次刷新，避免重复应用。
        pending = [e for e in entries
                   if self._last_xid < e[0] <= snapshot.xmax_snap]
        if during_snapshot:
            during_snapshot(snapshot)
        if not pending:
            if snapshot.xmax_snap != self._last_xid:
                # 高水位已前进却没有任何日志条目：日志整体丢失
                self._full_build(snapshot)
                return RefreshReport(
                    "full", snapshot, 0, 0,
                    "changelog 缺口：高水位 %d 但无视图未见的日志条目"
                    % snapshot.xmax_snap, len(self._groups))
            return RefreshReport("none", snapshot, 0, 0, None, len(self._groups))
        reason, row_changes = self._check_fallback(pending, snapshot)
        if reason is not None:
            # 回退：仍然用同一个一致性快照做全量重算
            self._full_build(snapshot)
            return RefreshReport("full", snapshot, row_changes, 0, reason,
                                 len(self._groups))
        try:
            for entry in pending:
                if entry[1] != "MARK":
                    self._apply_entry(entry)
        except InconsistentState as exc:
            # 增量状态与日志对不上：放弃半成品，全量重算
            self._full_build(snapshot)
            return RefreshReport("full", snapshot, row_changes, 0,
                                 "增量状态不一致：%s" % exc, len(self._groups))
        self._last_xid = snapshot.xmax_snap
        return RefreshReport("incremental", snapshot, row_changes, row_changes,
                             None, len(self._groups))

    # ---------- 读取 ----------

    def rebuild(self, snapshot=None):
        """无条件全量重算（对拍基准 / 手动重建用）。返回使用的快照。"""
        if snapshot is None:
            snapshot = self.table.snapshot()
        self._full_build(snapshot)
        return snapshot

    @staticmethod
    def _clean_top(heap, ctr, sign):
        while heap:
            top = heap[0] * sign
            if ctr.get(top, 0) > 0:
                return top
            heapq.heappop(heap)
        return None

    def rows(self):
        """物化视图当前内容：按分组键排序的行列表。"""
        out = []
        for gk in sorted(self._groups):
            st = self._groups[gk]
            mn = self._clean_top(st.min_heap, st.min_ctr, 1)
            mx = self._clean_top(st.max_heap, st.max_ctr, -1)
            out.append((gk, st.count, st.total, mn, mx))
        return out

    def rows_as_dicts(self):
        cols = (self.group_col, "cnt", "sum_%s" % self.sum_col,
                "min_%s" % self.sum_col, "max_%s" % self.sum_col)
        return [dict(zip(cols, r)) for r in self.rows()]


class InconsistentState(Exception):
    """增量状态与变更日志不一致，必须全量重算。"""
