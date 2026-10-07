"""运行记录器：把每次回调执行追加为一条 RunRecord。

开销上界：每次回调执行只增加 O(1) 工作（两次时钟读数、一次
list.append），不排序、不聚合、不分配额外对象；记录条数与回调
执行次数严格相等，因此内存占用随执行次数线性增长、可预估。
"""

from collections import namedtuple

# 一条回调执行记录。
#   reg_id/name/site : 回调注册标识（定位“是谁干的”）
#   ready_ts         : 入队时刻（就绪）
#   start_ts/end_ts  : 实际执行窗口 [start_ts, end_ts)
RunRecord = namedtuple(
    "RunRecord", ["reg_id", "name", "site", "ready_ts", "start_ts", "end_ts"]
)


class FairnessMonitor:
    def __init__(self):
        self.records = []
        self._pending = None  # (reg_id, name, site, ready_ts, start_ts)

    def _on_dispatch(self, reg_id, name, site, ready_ts, start_ts):
        self._pending = (reg_id, name, site, ready_ts, start_ts)

    def _on_complete(self, end_ts):
        reg_id, name, site, ready_ts, start_ts = self._pending
        self.records.append(
            RunRecord(reg_id, name, site, ready_ts, start_ts, end_ts)
        )
        self._pending = None

    def __len__(self):
        return len(self.records)
