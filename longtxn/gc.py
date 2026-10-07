"""垃圾清理：只回收水位线以下的冗余版本，绝不越过水位。

回收规则（对每个 key 的版本链，按 commit_seq 升序）：
  设 i = 满足 chain[i].commit_seq <= watermark 的最大下标。
  保留 chain[i]（最老活跃快照仍可能读它）及 chain[i+1:]（水位之上），
  回收 chain[0:i]（对任何现存与未来快照都不可见的旧版本）。

清理成本注入逻辑时钟：scan_cost/版本 + remove_cost/版本，
使"清理耗时"完全由测试注入的时钟度量，与墙钟无关。
"""

from dataclasses import dataclass
from typing import Callable, List, Optional

from .store import VersionStore
from .txn import TransactionManager


@dataclass
class GCStats:
    watermark: int
    keys_scanned: int = 0
    versions_scanned: int = 0
    versions_removed: int = 0
    logical_seconds: float = 0.0
    crashed: bool = False


class CrashInjected(Exception):
    """测试注入的崩溃：清理中途进程宕掉。"""


class GarbageCollector:
    def __init__(self, store: VersionStore, txns: TransactionManager,
                 scan_cost: float = 1e-6, remove_cost: float = 5e-6):
        self.store = store
        self.txns = txns
        self.scan_cost = scan_cost
        self.remove_cost = remove_cost

    def collect(self, watermark: Optional[int] = None,
                on_key: Optional[Callable[[str, int], None]] = None
                ) -> GCStats:
        wm = self.txns.watermark() if watermark is None else watermark
        stats = GCStats(watermark=wm)
        clock = self.txns.clock
        start = clock.now()
        for key in self.store.keys():
            if on_key is not None:
                on_key(key, stats.versions_removed)  # 可抛 CrashInjected
            chain = self.store.chain(key)
            stats.keys_scanned += 1
            stats.versions_scanned += len(chain)
            clock.advance(self.scan_cost * len(chain))
            keep_from = 0  # chain[0:keep_from] 可回收
            for i, v in enumerate(chain):
                if v.commit_seq <= wm:
                    keep_from = i  # 保留下标 i 本身，回收 [0, i)
                else:
                    break
            if keep_from > 0:
                removed = chain[:keep_from]
                self.store.replace_chain(key, chain[keep_from:])
                stats.versions_removed += len(removed)
                clock.advance(self.remove_cost * len(removed))
            stats.logical_seconds = clock.now() - start
        return stats
