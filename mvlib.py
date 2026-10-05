"""
mvlib.py — 物化视图增量刷新库（仅标准库）

模型
----
- BaseTable        : 基表，主键 -> 行(dict)。apply() 以“整体替换”方式应用变更，
                     因此 snapshot() 是 O(1) 的：拿到的是不可变引用，即一致快照。
- MaterializedView : 视图 = 过滤(filter_fn) + 分组(group_keys) + 聚合(aggs)。
                     支持聚合: count / sum / avg / min / max。

增量规则（对每条变更）
----------------------
设旧视图状态为 V，变更日志为 L（insert/update/delete）：

1. INSERT r :
     r 不满足 filter            -> 无影响；
     否则                       -> 定位组 g = group_keys(r)，
                                  count+=1, sum+=v, avg=sum/count,
                                  min/max 直接比较更新。
2. DELETE pk :
     旧行不在视图成员中          -> 无影响；
     否则按旧行做“反向贡献”：    count-=1, sum-=v, avg 重算；
                                  count 归 0 则整组删除。
3. UPDATE pk -> r' :
     等价于 DELETE(旧行) + INSERT(新行)。
     旧行与新行同组时仍按“先减后加”处理，保证组键变化（跨组移动）也被覆盖。

退回全量的条件（识别后不硬算）
------------------------------
F1. 视图含 min/max 聚合，且某次 DELETE/UPDATE 删除的贡献值恰好等于
    该组当前 min/max 且删除后组仍非空 —— 新极值无法由旧增量状态推出，
    识别后退回全量重算（抛 FallbackRequired，由 refresh 捕获）。
F2. 变更量超过阈值：len(changes) > fallback_ratio * 基表行数，
    增量不如全量，直接走全量。
F3. 变更日志中出现未知操作类型 —— 无法增量解释，退回全量。

一致性
------
refresh() 先在基表锁内取 (rows_snapshot, changelog) 二元组：
此后对基表的任何写入都不会出现在该快照里，刷新全程只读该快照；
快照之后到达的变更留在 changelog 中，供下一次 refresh 使用。
视图状态在计算完成后一次性原子替换，读者不会看到中间态。
"""

import threading


class FallbackRequired(Exception):
    """增量无法处理，必须退回全量。reason 字段说明原因。"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# 变更日志条目:
#   ('insert', row)          row 含 'pk'
#   ('update', pk, new_row)
#   ('delete', pk)
_VALID_OPS = {'insert', 'update', 'delete'}


class BaseTable:
    """基表。行一旦放入即视为不可变；更新 = 整行替换。因此快照零拷贝。"""

    def __init__(self, rows=None):
        self._lock = threading.Lock()
        self._rows = {}
        self._changelog = []
        if rows:
            init = {}
            for r in rows:
                init[r['pk']] = r
            self._rows = init

    def apply(self, changes):
        """批量应用变更。整体替换 _rows 引用，旧引用（快照）不受影响。"""
        with self._lock:
            new_rows = dict(self._rows)
            for ch in changes:
                op = ch[0]
                if op not in _VALID_OPS:
                    raise ValueError('unknown op: %r' % (op,))
                if op == 'insert':
                    row = ch[1]
                    new_rows[row['pk']] = row
                elif op == 'update':
                    pk, new_row = ch[1], ch[2]
                    if new_row.get('pk', pk) != pk:
                        raise ValueError('update 不允许修改主键')
                    new_rows[pk] = new_row
                else:  # delete
                    new_rows.pop(ch[1], None)
                self._changelog.append(ch)
            self._rows = new_rows

    def snapshot(self):
        """在锁内取一致快照：(行快照引用, 自上次快照以来的变更日志)。"""
        with self._lock:
            log = self._changelog
            self._changelog = []
            return self._rows, log

    def peek(self):
        """只读当前行集，不消费变更日志（对拍/校验用）。"""
        with self._lock:
            return self._rows

    def __len__(self):
        return len(self._rows)


class MaterializedView:
    """
    filter_fn(row) -> bool        过滤条件（必须确定性）
    group_keys: [col, ...]        分组列
    aggs: [('count', None), ('sum'|'avg'|'min'|'max', col), ...]
    fallback_ratio: 变更量/基表行数 超过该比例则退回全量
    """

    def __init__(self, base, name, filter_fn, group_keys, aggs,
                 fallback_ratio=0.5):
        self.base = base
        self.name = name
        self.filter_fn = filter_fn
        self.group_keys = list(group_keys)
        self.aggs = list(aggs)
        self.fallback_ratio = fallback_ratio
        self._lock = threading.Lock()
        self._groups = {}   # gkey(tuple) -> 累加器 dict
        self._members = {}  # pk -> (gkey, row)  仅含通过过滤的行
        self.last_refresh = None  # dict: mode/reason/changes/duration/rows_read

    # ---------- 内部：累加器 ----------

    def _new_state(self):
        st = {'count': 0}
        for fn, col in self.aggs:
            if fn in ('sum', 'avg'):
                st.setdefault('sum', {})[col] = 0.0
            elif fn == 'min':
                st.setdefault('min', {})[col] = None
            elif fn == 'max':
                st.setdefault('max', {})[col] = None
            elif fn != 'count':
                raise ValueError('unsupported agg: %r' % fn)
        return st

    def _gkey(self, row):
        return tuple(row[k] for k in self.group_keys)

    def _add(self, gkey, row):
        st = self._groups.get(gkey)
        if st is None:
            st = self._groups[gkey] = self._new_state()
        st['count'] += 1
        for col in st.get('sum', ()):
            st['sum'][col] += row[col]
        for col in st.get('min', ()):
            v = row[col]
            if st['min'][col] is None or v < st['min'][col]:
                st['min'][col] = v
        for col in st.get('max', ()):
            v = row[col]
            if st['max'][col] is None or v > st['max'][col]:
                st['max'][col] = v

    def _remove(self, gkey, row):
        st = self._groups[gkey]
        # F1: min/max 极值被删除且组仍非空 -> 无法增量，退回全量
        if st['count'] > 1:
            for col in st.get('min', ()):
                if row[col] == st['min'][col]:
                    raise FallbackRequired(
                        'min(%s) 极值被删除/改走，增量无法推出新极值' % col)
            for col in st.get('max', ()):
                if row[col] == st['max'][col]:
                    raise FallbackRequired(
                        'max(%s) 极值被删除/改走，增量无法推出新极值' % col)
        st['count'] -= 1
        for col in st.get('sum', ()):
            st['sum'][col] -= row[col]
        if st['count'] == 0:
            del self._groups[gkey]

    # ---------- 全量 ----------

    def _full_recompute(self, rows):
        groups, members = {}, {}
        for pk, row in rows.items():
            if not self.filter_fn(row):
                continue
            gk = self._gkey(row)
            st = groups.get(gk)
            if st is None:
                st = groups[gk] = self._new_state()
            # 复用 _add 的逻辑但作用在局部 groups 上
            st['count'] += 1
            for col in st.get('sum', ()):
                st['sum'][col] += row[col]
            for col in st.get('min', ()):
                v = row[col]
                if st['min'][col] is None or v < st['min'][col]:
                    st['min'][col] = v
            for col in st.get('max', ()):
                v = row[col]
                if st['max'][col] is None or v > st['max'][col]:
                    st['max'][col] = v
            members[pk] = (gk, row)
        return groups, members

    # ---------- 增量 ----------

    def _apply_incremental(self, changes):
        for ch in changes:
            op = ch[0]
            if op not in _VALID_OPS:
                raise FallbackRequired('未知变更操作: %r' % (op,))
            if op == 'insert':
                row = ch[1]
                if self.filter_fn(row):
                    gk = self._gkey(row)
                    self._add(gk, row)
                    self._members[row['pk']] = (gk, row)
            elif op == 'delete':
                old = self._members.pop(ch[1], None)
                if old is not None:
                    self._remove(old[0], old[1])
            else:  # update = delete(old) + insert(new)
                pk, new_row = ch[1], ch[2]
                old = self._members.pop(pk, None)
                if old is not None:
                    self._remove(old[0], old[1])
                if self.filter_fn(new_row):
                    gk = self._gkey(new_row)
                    self._add(gk, new_row)
                    self._members[pk] = (gk, new_row)

    # ---------- 刷新入口 ----------

    def refresh(self, after_snapshot_hook=None):
        """
        返回 dict: {mode: 'incremental'|'full', reason, changes, rows_read}
        after_snapshot_hook(rows, log): 测试用钩子，在拿到快照后、计算前调用，
        用于验证“刷新期间读到的是一致快照”。
        """
        import time
        t0 = time.perf_counter()
        rows, log = self.base.snapshot()
        if after_snapshot_hook is not None:
            after_snapshot_hook(rows, log)

        # F2: 变更量过大 -> 直接全量
        if len(log) > self.fallback_ratio * max(1, len(rows)):
            groups, members = self._full_recompute(rows)
            mode, reason = 'full', ('变更量 %d 超过阈值 %.0f%%，退回全量'
                                    % (len(log), self.fallback_ratio * 100))
        else:
            try:
                # 在副本上试算，失败不污染旧状态
                old_groups, old_members = self._groups, self._members
                self._groups = {k: self._copy_state(v)
                                for k, v in old_groups.items()}
                self._members = dict(old_members)
                self._apply_incremental(log)
                groups, members = self._groups, self._members
                mode, reason = 'incremental', ''
            except FallbackRequired as e:
                groups, members = self._full_recompute(rows)
                mode, reason = 'full', e.reason
            finally:
                self._groups, self._members = old_groups, old_members

        with self._lock:
            self._groups, self._members = groups, members
            self.last_refresh = {
                'mode': mode, 'reason': reason, 'changes': len(log),
                'rows_read': len(rows),
                'duration': time.perf_counter() - t0,
            }
        return self.last_refresh

    @staticmethod
    def _copy_state(st):
        return {k: (dict(v) if isinstance(v, dict) else v)
                for k, v in st.items()}

    # ---------- 读取 ----------

    def rows(self):
        """视图当前内容：按组键排序的行列表。"""
        with self._lock:
            out = []
            for gk in sorted(self._groups):
                st = self._groups[gk]
                row = {k: v for k, v in zip(self.group_keys, gk)}
                for fn, col in self.aggs:
                    alias = fn if col is None else '%s_%s' % (fn, col)
                    if fn == 'count':
                        row[alias] = st['count']
                    elif fn == 'sum':
                        row[alias] = st['sum'][col]
                    elif fn == 'avg':
                        row[alias] = st['sum'][col] / st['count']
                    elif fn == 'min':
                        row[alias] = st['min'][col]
                    elif fn == 'max':
                        row[alias] = st['max'][col]
                out.append(row)
            return out

    def full_recompute_rows(self):
        """独立的全量重算（不动视图状态），用于对拍。"""
        return self._full_rows_from(self.base.peek())

    def _full_rows_from(self, rows):
        groups, _ = self._full_recompute(rows)
        saved_g, saved_m = self._groups, self._members
        try:
            self._groups = groups
            return self.rows()
        finally:
            self._groups = saved_g
