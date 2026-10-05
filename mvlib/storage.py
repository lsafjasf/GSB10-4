"""极简内存表，自带 MVCC 版本链 + 仅追加变更日志（changelog）。

只依赖 Python 标准库。

存储模型
--------
每个主键对应一条 *版本链*，链上每个版本是一个元组：

    (xmin, xmax, dict 行内容或 None)

- xmin：创建该版本的事务号（版本号从 1 开始单调递增）。
- xmax：删除/覆盖该版本的事务号；None 表示未被删除。
- 行内容为 None 的版本是「墓碑」版本，仅用于记录删除事件（保证扫描稳定，
  见 Snapshot.iter_rows 的说明）。

快照 S 是一个已提交事务号集合，加上一个快照生成时的高水位 S_max。
某版本 v 对快照 S 可见，当且仅当：

    v.xmin in S 且 (v.xmax is None 或 v.xmax not in S)

写操作（insert/update/delete）以一个事务号提交：只向版本链尾部追加，
从不原地修改旧版本，因此旧快照永远可重复读。

变更日志
--------
changelog 也是仅追加的，每条记录：

    (xid, op, pk, before, after)

op ∈ {"INSERT","UPDATE","DELETE"}，before/after 为行 dict 或 None。
批量装载（bulk_load）不写逐行日志，只打一个 ("BOUNDARY",) 标记，
因为物化视图的基线直接建在批量装载之后的快照上。
表清空（truncate）打 ("TRUNCATE",) 标记，schema 变更打 ("SCHEMA",) 标记；
这两类变化无法按行增量处理，刷新器遇到后必须退回全量重算。

并发
----
一把可重入锁保护所有提交与快照创建。提交只做追加操作，快照迭代器拿到
引用后无需持锁遍历，刷新期间其它事务照样可以提交。
"""

import threading

# changelog 中非逐行的特殊标记
BOUNDARY = "BOUNDARY"
TRUNCATE_MARK = "TRUNCATE"
SCHEMA_MARK = "SCHEMA"

_INSERT = "INSERT"
_UPDATE = "UPDATE"
_DELETE = "DELETE"
_MARK = "MARK"


class Snapshot:
    """某一刻已提交事务号集合形成的一致性只读快照。

    本实现中提交在锁内原子完成，任一时刻「已提交集合」恒为 1..高水位 的
    前缀，因此快照只需记录高水位，可见性用一次整数比较即可判定。
    """

    __slots__ = ("xmax_snap", "xid")

    def __init__(self, xmax_snap, xid):
        # 快照生成时最新已提交事务号（高水位）
        self.xmax_snap = xmax_snap
        # 快照自身的编号，仅用于报告展示
        self.xid = xid

    def visible(self, version):
        xmin, xmax, _ = version
        return (xmin <= self.xmax_snap
                and (xmax is None or xmax > self.xmax_snap))

    def iter_rows(self, rows, table_lock):
        """按主键顺序产出 (pk, 行dict)。

        rows 是 dict，迭代期间写事务只做 dict[key].append / dict[key]=新链，
        两者在 CPython 下都是原子操作，且我们绝不原地改旧版本，所以无需持锁
        也不会读到「半更新」状态；可见性一律由本快照的 xids 决定。
        """
        for pk in sorted(rows.keys()):
            chain = rows[pk]
            chosen = None
            for version in reversed(chain):
                if self.visible(version):
                    chosen = version
                    break
            if chosen is not None and chosen[2] is not None:
                yield pk, chosen[2]

    def describe(self):
        return {
            "snapshot_xid": self.xid,
            "committed_high_watermark": self.xmax_snap,
            "committed_tx_count": self.xmax_snap,
        }


class Table:
    """带版本链与变更日志的内存表。"""

    def __init__(self, name, key="id"):
        self.name = name
        self.key = key
        self.columns = None          # 有序列名（首次写入时确定）
        self._rows = {}              # pk -> list[(xmin, xmax, payload)]
        self._log = []               # 仅追加 changelog
        self._next_xid = 1
        self._live_count = 0         # 最新提交视角下的存活行数（仅用于成本估算）
        self._lock = threading.RLock()

    # ---------- 快照 ----------

    def snapshot(self):
        with self._lock:
            return Snapshot(self._next_xid - 1, self._next_xid)

    def iter_snapshot(self, snapshot):
        return snapshot.iter_rows(self._rows, self._lock)

    def snapshot_row_count(self, snapshot):
        return sum(1 for _ in self.iter_snapshot(snapshot))

    def live_count(self):
        with self._lock:
            return self._live_count

    # ---------- 写事务 ----------

    def _new_xid(self):
        xid = self._next_xid
        self._next_xid += 1
        return xid

    def _check_columns(self, row):
        cols = tuple(sorted(row.keys()))
        if self.columns is None:
            self.columns = cols
        elif cols != self.columns:
            raise ValueError(
                "schema 不一致：期望列 %s，实际列 %s" % (self.columns, cols)
            )

    def bulk_load(self, records):
        """批量装载初始数据。不写逐行日志，只打 BOUNDARY 标记。

        用于建立物化视图的基线；基线之后才允许按行增量维护。
        """
        with self._lock:
            if self._rows:
                raise RuntimeError("bulk_load 只能在空表上执行一次")
            xid = self._new_xid()
            n = 0
            for row in records:
                self._check_columns(row)
                pk = row[self.key]
                if pk in self._rows:
                    raise ValueError("bulk_load 主键冲突: %r" % (pk,))
                self._rows[pk] = [(xid, None, dict(row))]
                n += 1
            self._live_count = n
            self._log.append((xid, _MARK, BOUNDARY, None, None))
            return xid

    def insert(self, row):
        with self._lock:
            self._check_columns(row)
            pk = row[self.key]
            xid = self._new_xid()
            chain = self._rows.get(pk)
            if chain is not None and chain[-1][2] is not None and chain[-1][1] is None:
                raise ValueError("主键已存在: %r" % (pk,))
            payload = dict(row)
            if chain is None:
                self._rows[pk] = [(xid, None, payload)]
            else:
                chain.append((xid, None, payload))
            self._live_count += 1
            self._log.append((xid, _INSERT, pk, None, payload))
            return xid

    def update(self, pk, changes):
        """更新一行：changes 给出要修改的列（可含任意列，主键不可改）。"""
        with self._lock:
            chain = self._rows.get(pk)
            if chain is None or chain[-1][2] is None or chain[-1][1] is not None:
                raise KeyError("待更新行不存在: %r" % (pk,))
            xid = self._new_xid()
            old = chain[-1][2]
            if self.key in changes and changes[self.key] != pk:
                raise ValueError("不允许修改主键")
            new = dict(old)
            new.update(changes)
            self._check_columns(new)
            chain[-1] = (chain[-1][0], xid, old)   # 旧版本封版
            chain.append((xid, None, new))
            self._log.append((xid, _UPDATE, pk, old, new))
            return xid

    def delete(self, pk):
        with self._lock:
            chain = self._rows.get(pk)
            if chain is None or chain[-1][2] is None or chain[-1][1] is not None:
                raise KeyError("待删除行不存在: %r" % (pk,))
            xid = self._new_xid()
            old = chain[-1][2]
            chain[-1] = (chain[-1][0], xid, old)
            chain.append((xid, None, None))        # 墓碑版本
            self._live_count -= 1
            self._log.append((xid, _DELETE, pk, old, None))
            return xid

    def truncate(self):
        """清空表：存量视图结果全部失效，无法按行增量处理。"""
        with self._lock:
            xid = self._new_xid()
            for chain in self._rows.values():
                if chain[-1][2] is not None and chain[-1][1] is None:
                    old = chain[-1][2]
                    chain[-1] = (chain[-1][0], xid, old)
                    chain.append((xid, None, None))
            self._live_count = 0
            self._log.append((xid, _MARK, TRUNCATE_MARK, None, None))
            return xid

    def alter_schema(self):
        """模拟 schema 变更（加列等）：列语义改变，旧视图必须全量重建。"""
        with self._lock:
            xid = self._new_xid()
            self._log.append((xid, _MARK, SCHEMA_MARK, None, None))
            return xid

    # ---------- 变更日志读取（供增量刷新器使用） ----------

    def log_tail(self, since_xid):
        """返回 (条目列表, 最新xid)。

        条目元组：(xid, op, pk, before, after)
        特殊标记 op=="MARK"，pk 为 BOUNDARY/TRUNCATE_MARK/SCHEMA_MARK。
        仅追加，所以加锁复制引用即可；返回副本避免调用方受后续追加影响。
        """
        with self._lock:
            return list(self._log), self._next_xid - 1

    def debug_corrupt_log_gap(self):
        """测试专用：删掉最新的一条逐行变更，制造日志缺口，模拟日志丢失。"""
        with self._lock:
            for i in range(len(self._log) - 1, -1, -1):
                if self._log[i][1] != _MARK:
                    return self._log.pop(i)
            raise RuntimeError("没有可删除的逐行日志")
