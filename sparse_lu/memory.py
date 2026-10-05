"""内存守卫：分解过程中周期性估算存活数据量并检查进程 RSS，超预算立即抛异常。

这样做而不是等 malloc 失败：可以在系统 OOM killer 介入之前，
由库主动、明确地拒绝（MemoryBudgetExceeded），进程不被杀掉。
"""

import os
import resource

# 行字典里每个 (int键, float值) 条目的经验字节数（dict 槽位 + 键值对象 + 集合镜像开销）。
# 取保守上界，宁可早拒绝。
DEFAULT_BYTES_PER_ENTRY = 144


class MemoryBudgetExceeded(RuntimeError):
    """预计/实际内存超过预算时抛出。调用方应捕获并给出明确处理（拒绝/减小规模/溢写）。"""

    def __init__(self, message, live_entries=None, est_bytes=None,
                 rss_bytes=None, limit_bytes=None, context=""):
        super().__init__(message)
        self.live_entries = live_entries
        self.est_bytes = est_bytes
        self.rss_bytes = rss_bytes
        self.limit_bytes = limit_bytes
        self.context = context


def current_rss_bytes():
    """当前常驻内存（字节）。Linux 读 /proc；不支持时返回 None。"""
    try:
        with open("/proc/self/status", "r") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def max_rss_bytes():
    """进程峰值 RSS（字节）。Linux/macOS 均支持，单位换算略有差异。"""
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if kb <= 0:
        return None
    return kb * 1024


class MemoryGuard:
    def __init__(self, limit_bytes, bytes_per_entry=DEFAULT_BYTES_PER_ENTRY):
        self.limit_bytes = int(limit_bytes)
        self.bytes_per_entry = bytes_per_entry
        self.baseline_rss = current_rss_bytes() or 0
        self.peak_entries = 0
        self.checks = 0

    def check(self, live_entries, context=""):
        self.checks += 1
        self.peak_entries = max(self.peak_entries, live_entries)
        est_bytes = live_entries * self.bytes_per_entry + self.baseline_rss
        rss = current_rss_bytes()
        used = est_bytes if rss is None else max(est_bytes, rss)
        if used > self.limit_bytes:

            def mb(x):
                return f"{x / (1024 * 1024):.1f} MiB"

            raise MemoryBudgetExceeded(
                "内存预算超限，已安全拒绝继续分解"
                + (f"（阶段: {context}）" if context else "")
                + f": 存活条目 {live_entries}, 估算 {mb(est_bytes)}, "
                f"RSS {mb(rss) if rss is not None else 'n/a'}, "
                f"预算 {mb(self.limit_bytes)}。建议：1) 换 RCM/最小度重排；"
                "2) 开启溢写(spill_dir)；3) 调大块大小不能解决填充爆炸，应降低规模或提高预算。",
                live_entries=live_entries,
                est_bytes=est_bytes,
                rss_bytes=rss,
                limit_bytes=self.limit_bytes,
                context=context,
            )
