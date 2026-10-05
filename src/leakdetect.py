"""leakdetect: 句柄/连接泄漏检测库（仅标准库）。

核心思想：不按"泄漏总数"报告，而是按【获取点（调用栈/标识）】归因。
每个被跟踪的句柄/连接在 acquire 时记录获取点与获取时间，release 时核销；
判定泄漏时给出三重依据：
  1. 引用计数依据：该句柄在跟踪表中仍有 1 条未核销的获取记录（acquire - release > 0）；
  2. 存活时长依据：now - acquired_at >= age_threshold（排除仍在正常使用中的句柄）；
  3. 宽限确认（可选）：confirm() 在 grace_period 后复查仍存活的记录，排除"恰好即将释放"的误报。
"""

from __future__ import annotations

import linecache
import sys
import threading
import time
import traceback
from collections import defaultdict

__all__ = [
    "LeakTracker",
    "LeakError",
    "DoubleReleaseError",
    "UnknownHandleError",
    "LeakRecord",
    "SiteStats",
]

# 过滤本库内部帧，使归因指向调用方代码
_INTERNAL_MARKERS = ("/leakdetect.py", "\\leakdetect.py")


class LeakError(Exception):
    """泄漏检测相关错误基类。"""


class DoubleReleaseError(LeakError):
    """对同一获取记录重复 release。"""


class UnknownHandleError(LeakError):
    """release 了一个从未被 acquire 记录的句柄。"""


class LeakRecord:
    """一条泄漏记录：归因到获取点，并携带判定依据。"""

    __slots__ = ("handle", "site", "acquired_at", "age", "age_ok", "refcount_ok",
                 "exception", "acquire_stack")

    def __init__(self, handle, site, acquired_at, age, age_ok, refcount_ok,
                 exception=None, acquire_stack=None):
        self.handle = handle
        self.site = site                  # 获取点标识 "file:lineno func"
        self.acquired_at = acquired_at    # 获取时间（monotonic）
        self.age = age                    # 判定时的存活时长（秒）
        self.age_ok = age_ok              # 依据1：存活时长超过阈值
        self.refcount_ok = refcount_ok    # 依据2：跟踪表中引用计数（未核销获取）> 0
        self.exception = exception        # 获取后、释放前发生的异常（异常路径泄漏）
        self.acquire_stack = acquire_stack  # 获取点完整调用栈（可选）

    def __repr__(self):
        return (f"LeakRecord(site={self.site!r}, age={self.age:.3f}s, "
                f"age_ok={self.age_ok}, refcount_ok={self.refcount_ok}, "
                f"exception={self.exception!r})")


class SiteStats:
    """按获取点聚合的核算数据。"""

    __slots__ = ("acquired", "released", "double_releases", "leaked")

    def __init__(self):
        self.acquired = 0
        self.released = 0
        self.double_releases = 0
        self.leaked = 0

    def to_dict(self):
        return {
            "acquired": self.acquired,
            "released": self.released,
            "leaked": self.leaked,
            "double_releases": self.double_releases,
            "check": f"{self.acquired} - {self.released} = {self.acquired - self.released}",
        }


def _caller_site(stacklevel=1):
    """返回调用方（库外）的获取点标识。stacklevel 为库内部额外层数。"""
    frame = sys._getframe(stacklevel)
    try:
        while frame is not None and frame.f_code.co_filename.endswith(_INTERNAL_MARKERS):
            frame = frame.f_back
        if frame is None:
            return "<unknown>"
        return f"{frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}"
    finally:
        del frame


def _capture_stack():
    """捕获调用方（库外）完整调用栈，含源码行。"""
    stack = traceback.extract_stack()
    frames = [f for f in stack if not f.filename.endswith(_INTERNAL_MARKERS)]
    lines = []
    for f in frames:
        src = f.line or (linecache.getline(f.filename, f.lineno).strip() or "<no source>")
        lines.append(f'  File "{f.filename}", line {f.lineno}, in {f.name}\n    {src}')
    return "\n".join(lines)


class LeakTracker:
    """句柄/连接泄漏跟踪器。

    参数:
        age_threshold: 存活时长阈值（秒）。超过该时长仍未释放才判定为泄漏，
                       用于排除"正在正常使用中"的误报。
        capture_stacks: 是否为每次获取保存完整调用栈（开销更大，默认开）。
        on_double_release: 重复释放策略，"raise"（默认）或 "record"。
    """

    def __init__(self, name="tracker", age_threshold=5.0,
                 capture_stacks=True, on_double_release="raise"):
        if on_double_release not in ("raise", "record"):
            raise ValueError("on_double_release must be 'raise' or 'record'")
        self.name = name
        self.age_threshold = float(age_threshold)
        self.capture_stacks = capture_stacks
        self.on_double_release = on_double_release

        self._lock = threading.Lock()
        self._live = {}                 # handle_id -> (handle, site, acquired_at, exception, stack)
        self._released_sites = _BoundedMap(4096)  # 最近已核销句柄 id -> 获取点
        self._site_stats = defaultdict(SiteStats)
        self._stacks = {}               # site -> 代表性调用栈（去重存储，控制内存）
        self._acquired = 0
        self._released = 0
        self._double_releases = 0
        self._unknown_releases = 0
        self._exceptions = 0            # 异常路径下发生的释放次数
        self._normal_releases = 0       # 正常路径下的释放次数

    # ------------------------------------------------------------------ API

    def acquire(self, handle, site=None):
        """记录一次获取。site 缺省时自动取调用方代码位置。"""
        if site is None:
            site = _caller_site(stacklevel=2)
        now = time.monotonic()
        stack = _capture_stack() if self.capture_stacks else None
        with self._lock:
            self._live[id(handle)] = (handle, site, now, None, stack)
            self._acquired += 1
            st = self._site_stats[site]
            st.acquired += 1
            if stack is not None and site not in self._stacks:
                self._stacks[site] = stack
        return handle

    def release(self, handle, exception=None):
        """核销一次释放。

        exception: 若释放发生在异常路径（except/finally 中），传入异常对象，
                   该次释放计入"异常路径释放"统计。
        重复释放或未知句柄按 on_double_release 策略抛错或仅记录。
        """
        key = id(handle)
        with self._lock:
            rec = self._live.pop(key, None)
            if rec is None:
                self._note_bad_release(key, handle)
                return False
            self._released_sites[key] = rec[1]
            self._released += 1
            if exception is None:
                self._normal_releases += 1
            else:
                self._exceptions += 1
            self._site_stats[rec[1]].released += 1
            return True

    def _note_bad_release(self, key, handle):
        # 区分"重复释放"与"未知句柄"：曾被获取过且已核销 => 重复释放
        site = self._released_sites.get(key)
        if site is not None:
            self._double_releases += 1
            self._site_stats[site].double_releases += 1
            if self.on_double_release == "raise":
                raise DoubleReleaseError(f"handle {handle!r} released twice (site={site})")
        else:
            self._unknown_releases += 1
            if self.on_double_release == "raise":
                raise UnknownHandleError(f"handle {handle!r} was never acquired")

    def note_exception(self, handle, exc):
        """在异常路径上给句柄标注异常信息（泄漏报告中会携带）。"""
        with self._lock:
            rec = self._live.get(id(handle))
            if rec is not None:
                self._live[id(handle)] = (rec[0], rec[1], rec[2], repr(exc), rec[4])

    def tracked(self, handle):
        """该句柄当前是否处于被跟踪（获取未释放）状态。"""
        with self._lock:
            return id(handle) in self._live

    # ------------------------------------------------------------- 判定与报告

    def check(self, now=None, include_stack=True):
        """判定当前泄漏。返回 LeakRecord 列表，按获取点排序。

        判定依据（两条同时满足才计入）：
          age_ok:       存活时长 >= age_threshold
          refcount_ok:  跟踪表中该句柄仍有未核销的获取记录（恒为 True，
                        因为 _live 只保存未核销记录；显式列出以便审计）
        """
        if now is None:
            now = time.monotonic()
        leaks = []
        with self._lock:
            items = list(self._live.items())
        for _key, (handle, site, acquired_at, exc, stack) in items:
            age = now - acquired_at
            age_ok = age >= self.age_threshold
            refcount_ok = True  # 存在于 _live 即引用计数(未核销获取) = 1 > 0
            if age_ok and refcount_ok:
                leaks.append(LeakRecord(
                    handle=handle, site=site, acquired_at=acquired_at, age=age,
                    age_ok=age_ok, refcount_ok=refcount_ok, exception=exc,
                    acquire_stack=(stack if include_stack else None)))
        leaks.sort(key=lambda r: (r.site, r.acquired_at))
        return leaks

    def confirm(self, grace_period=0.2):
        """宽限确认：等待 grace_period 后复查，排除"即将正常释放"的误报。

        返回 (confirmed_leaks, cleared)。cleared 为首次判定疑似、宽限期内
        已被正常释放的记录数（即被排除的误报）。
        """
        suspects = {id(r.handle): r for r in self.check()}
        if not suspects:
            return [], 0
        time.sleep(grace_period)
        confirmed, cleared = [], 0
        for key, rec in suspects.items():
            with self._lock:
                still_live = key in self._live
            if still_live:
                confirmed.append(rec)
            else:
                cleared += 1
        return confirmed, cleared

    def stats(self, now=None):
        """核算数据：全局 + 按获取点。保证 acquired - released == live。"""
        if now is None:
            now = time.monotonic()
        with self._lock:
            live = len(self._live)
            per_site = {}
            for site, st in self._site_stats.items():
                per_site[site] = st.to_dict()
            # 重新核算每个 site 的当前泄漏数（= 未释放记录数）
            live_by_site = defaultdict(int)
            oldest_by_site = {}
            for handle, site, acquired_at, exc, stack in self._live.values():
                live_by_site[site] += 1
                age = now - acquired_at
                if site not in oldest_by_site or age > oldest_by_site[site]:
                    oldest_by_site[site] = age
            for site, d in per_site.items():
                d["leaked"] = live_by_site.get(site, 0)
                d["oldest_age_sec"] = round(oldest_by_site.get(site, 0.0), 6)
            result = {
                "tracker": self.name,
                "acquired": self._acquired,
                "released": self._released,
                "live": live,
                "leaked": live,
                "balance": f"{self._acquired} - {self._released} = {self._acquired - self._released}",
                "balanced": (self._acquired - self._released) == live,
                "normal_releases": self._normal_releases,
                "exception_path_releases": self._exceptions,
                "double_releases": self._double_releases,
                "unknown_releases": self._unknown_releases,
                "age_threshold_sec": self.age_threshold,
                "per_site": per_site,
            }
        return result

    def report(self, now=None, include_stack=True):
        """生成文本归因报告。"""
        leaks = self.check(now=now, include_stack=include_stack)
        s = self.stats(now=now)
        out = []
        out.append(f"=== 泄漏归因报告 [{s['tracker']}] ===")
        out.append(f"核算: acquired={s['acquired']} released={s['released']} "
                   f"live={s['live']}  ({s['balance']}, balanced={s['balanced']})")
        out.append(f"释放路径: 正常={s['normal_releases']} 异常={s['exception_path_releases']} "
                   f"重复释放={s['double_releases']} 未知句柄={s['unknown_releases']}")
        out.append(f"判定依据: 存活时长阈值={s['age_threshold_sec']}s 且 跟踪表引用计数>0")
        out.append("")
        if not leaks:
            out.append("无泄漏。")
        else:
            out.append(f"泄漏 {len(leaks)} 条，按获取点归因：")
            by_site = {}
            for r in leaks:
                by_site.setdefault(r.site, []).append(r)
            for i, (site, records) in enumerate(sorted(by_site.items()), 1):
                out.append(f"  [{i}] 获取点: {site}  (泄漏 {len(records)} 条)")
                ages = [r.age for r in records]
                out.append(f"      存活时长: {min(ages):.3f}s ~ {max(ages):.3f}s "
                           f"(阈值 {s['age_threshold_sec']}s, age_ok=True, 引用计数>0)")
                excs = sorted({r.exception for r in records if r.exception})
                for e in excs:
                    out.append(f"      关联异常: {e}")
                st = records[0].acquire_stack
                if st:
                    out.append("      获取调用栈(代表):")
                    out.append("\n".join("      " + ln for ln in st.splitlines()))
        out.append("")
        out.append("按获取点聚合：")
        for site, d in sorted(s["per_site"].items()):
            out.append(f"  {site}: acquired={d['acquired']} released={d['released']} "
                       f"leaked={d['leaked']} oldest_age={d['oldest_age_sec']}s "
                       f"double_releases={d['double_releases']}")
        return "\n".join(out)

    # ------------------------------------------------------------- 便捷封装

    def wrap(self, handle, site=None):
        """返回一个上下文管理器，with 退出时自动 release（含异常路径）。"""
        return _TrackedHandle(self, self.acquire(handle, site=site))


class _TrackedHandle:
    __slots__ = ("_tracker", "_handle", "_closed")

    def __init__(self, tracker, handle):
        self._tracker = tracker
        self._handle = handle
        self._closed = False

    @property
    def raw(self):
        return self._handle

    def __enter__(self):
        return self._handle

    def __exit__(self, exc_type, exc, tb):
        self.close(exception=exc)
        return False

    def close(self, exception=None):
        if not self._closed:
            self._closed = True
            self._tracker.release(self._handle, exception=exception)


class _BoundedMap:
    """有界字典：超出容量时丢弃最旧的一半，避免内存无界增长。"""

    def __init__(self, capacity):
        self._capacity = capacity
        self._keys = []
        self._data = {}

    def __setitem__(self, key, value):
        if key in self._data:
            self._data[key] = value
            return
        if len(self._keys) >= self._capacity:
            drop = self._keys[: self._capacity // 2]
            self._keys = self._keys[self._capacity // 2:]
            for d in drop:
                self._data.pop(d, None)
        self._keys.append(key)
        self._data[key] = value

    def get(self, key):
        return self._data.get(key)
