"""leakdetect 自测：覆盖 全部正常释放 / 部分泄漏 / 异常路径泄漏 / 重复释放 / 误报排除 / 核算。

运行: python3 test_leakdetect.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from leakdetect import (LeakTracker, DoubleReleaseError, UnknownHandleError)


class FakeHandle:
    """模拟句柄/连接。"""

    _seq = 0

    def __init__(self, kind="handle"):
        FakeHandle._seq += 1
        self.kind = kind
        self.no = FakeHandle._seq

    def __repr__(self):
        return f"<{self.kind}#{self.no}>"


def site_normal_acquire(tracker, n):
    """获取点A：正常获取并释放。"""
    for _ in range(n):
        h = tracker.acquire(FakeHandle("conn"))
        tracker.release(h)


def site_leaky_acquire(tracker, n):
    """获取点B：获取后故意不释放（泄漏点）。"""
    leaked = []
    for _ in range(n):
        leaked.append(tracker.acquire(FakeHandle("conn")))
    return leaked


def site_exception_acquire(tracker, n, release_in_finally):
    """获取点C：获取后抛异常。release_in_finally=False 时异常路径泄漏。"""
    leaked = []
    for _ in range(n):
        h = tracker.acquire(FakeHandle("conn"))
        try:
            raise RuntimeError("simulated backend failure")
        except RuntimeError as exc:
            tracker.note_exception(h, exc)
            if release_in_finally:
                tracker.release(h, exception=exc)  # 异常路径下的释放
            else:
                leaked.append(h)                   # 异常路径泄漏
    return leaked


def test_all_released():
    t = LeakTracker("t1", age_threshold=0.05)
    site_normal_acquire(t, 100)
    time.sleep(0.06)
    leaks = t.check()
    s = t.stats()
    assert leaks == [], f"应无泄漏, got {leaks}"
    assert s["acquired"] == 100 and s["released"] == 100 and s["live"] == 0
    assert s["balanced"] and s["normal_releases"] == 100
    print("[PASS] 场景1 全部正常释放: 0 泄漏, 核算 "
          f"{s['balance']}, 正常路径释放={s['normal_releases']}")
    return s


def test_partial_leak():
    t = LeakTracker("t2", age_threshold=0.05)
    site_normal_acquire(t, 50)
    leaked = site_leaky_acquire(t, 3)
    time.sleep(0.06)
    leaks = t.check()
    assert len(leaks) == 3, f"应检出 3 条泄漏, got {len(leaks)}"
    sites = {r.site for r in leaks}
    assert len(sites) == 1 and "site_leaky_acquire" in sites.pop(), sites
    for r in leaks:
        assert r.age_ok and r.refcount_ok and r.age >= 0.05
        assert r.acquire_stack and "site_leaky_acquire" in r.acquire_stack
    s = t.stats()
    assert s["acquired"] == 53 and s["released"] == 50 and s["live"] == 3
    assert s["balanced"]
    leaky_site = [k for k in s["per_site"] if "site_leaky_acquire" in k][0]
    d = s["per_site"][leaky_site]
    assert d["acquired"] == 3 and d["released"] == 0 and d["leaked"] == 3
    print(f"[PASS] 场景2 部分泄漏: 3 条泄漏全部归因到 {leaky_site}")
    print("       判定依据: " +
          "; ".join(f"age={r.age:.3f}s>=阈值, 引用计数>0" for r in leaks[:1]))
    # 清理，避免影响后续
    for h in leaked:
        t.release(h)
    return s


def test_exception_path():
    t = LeakTracker("t3", age_threshold=0.05)
    # 异常路径下正确释放 2 次
    site_exception_acquire(t, 2, release_in_finally=True)
    # 异常路径泄漏 2 次
    leaked = site_exception_acquire(t, 2, release_in_finally=False)
    time.sleep(0.06)
    leaks = t.check()
    assert len(leaks) == 2, f"应检出 2 条异常路径泄漏, got {len(leaks)}"
    for r in leaks:
        assert r.exception and "simulated backend failure" in r.exception
        assert "site_exception_acquire" in r.site
    s = t.stats()
    assert s["exception_path_releases"] == 2, s
    assert s["normal_releases"] == 0
    assert s["acquired"] == 4 and s["released"] == 2 and s["live"] == 2 and s["balanced"]
    print(f"[PASS] 场景3 异常路径: 异常路径释放={s['exception_path_releases']}, "
          f"异常路径泄漏=2 (泄漏记录携带异常信息: {leaks[0].exception!r})")
    for h in leaked:
        t.release(h)
    return s


def test_double_release():
    # 策略1: raise
    t = LeakTracker("t4a", age_threshold=0.05)
    h = t.acquire(FakeHandle())
    t.release(h)
    try:
        t.release(h)
        raise AssertionError("重复释放应抛 DoubleReleaseError")
    except DoubleReleaseError:
        pass
    try:
        t.release(FakeHandle())
        raise AssertionError("未知句柄应抛 UnknownHandleError")
    except UnknownHandleError:
        pass
    s = t.stats()
    assert s["double_releases"] == 1 and s["unknown_releases"] == 1
    assert s["acquired"] == 1 and s["released"] == 1 and s["live"] == 0 and s["balanced"]

    # 策略2: record（不抛错，只计数）
    t2 = LeakTracker("t4b", age_threshold=0.05, on_double_release="record")
    h2 = t2.acquire(FakeHandle())
    t2.release(h2)
    assert t2.release(h2) is False   # 重复释放被记录
    s2 = t2.stats()
    assert s2["double_releases"] == 1 and s2["balanced"]
    print(f"[PASS] 场景4 重复释放: raise 策略抛错并计数(双释放={s['double_releases']}, "
          f"未知句柄={s['unknown_releases']}); record 策略计数={s2['double_releases']}")
    return s


def test_false_positive_exclusion():
    import threading
    t = LeakTracker("t5", age_threshold=0.2)

    # 误报排除机制1（存活时长依据）：句柄仍在正常使用中、未超阈值，不判为泄漏
    h_young = t.acquire(FakeHandle())
    assert t.check() == [], "未超存活阈值的句柄不应判为泄漏"
    t.release(h_young)

    # 误报排除机制2（宽限确认）：疑似泄漏在 grace_period 内被正常释放 => 排除
    h_grace = t.acquire(FakeHandle())
    time.sleep(0.25)  # 越过阈值，首次判定为疑似泄漏
    assert len(t.check()) == 1

    def release_soon():
        time.sleep(0.05)
        t.release(h_grace)  # 宽限期内完成释放
    threading.Thread(target=release_soon).start()
    confirmed, cleared = t.confirm(grace_period=0.15)
    assert cleared == 1, f"宽限期内被释放的误报应被排除, cleared={cleared}"
    assert confirmed == [], f"不应有确认真实泄漏, got {confirmed}"

    # 对照：真正泄漏的句柄在宽限期后仍存活
    h_real = t.acquire(FakeHandle())
    time.sleep(0.25)
    confirmed, cleared = t.confirm(grace_period=0.05)
    assert len(confirmed) == 1 and confirmed[0].handle is h_real and cleared == 0
    t.release(h_real)

    s = t.stats()
    assert s["balanced"] and s["live"] == 0
    print(f"[PASS] 场景5 误报排除: 阈值内不判泄漏; 宽限确认排除误报1条, 确认真实泄漏1条")
    return s


def test_context_manager():
    t = LeakTracker("t6", age_threshold=0.05)
    # 正常路径
    with t.wrap(FakeHandle()) as h:
        pass
    # 异常路径：with 退出时自动按异常路径释放
    try:
        with t.wrap(FakeHandle()) as h:
            raise ValueError("boom")
    except ValueError:
        pass
    s = t.stats()
    assert s["acquired"] == 2 and s["released"] == 2 and s["live"] == 0
    assert s["normal_releases"] == 1 and s["exception_path_releases"] == 1
    print(f"[PASS] 场景6 上下文管理器: 正常释放={s['normal_releases']}, "
          f"异常路径释放={s['exception_path_releases']}, 核算 {s['balance']}")
    return s


def test_accounting():
    t = LeakTracker("t7", age_threshold=0.05)
    site_normal_acquire(t, 700)
    leaked = site_leaky_acquire(t, 5)
    site_exception_acquire(t, 3, release_in_finally=True)
    time.sleep(0.06)  # 越过存活阈值，使报告实际展示泄漏归因
    s = t.stats()
    assert s["acquired"] == 708 and s["released"] == 703 and s["live"] == 5
    assert s["balanced"], s
    assert s["acquired"] - s["released"] == s["leaked"]
    print(f"[PASS] 场景7 核算: acquired={s['acquired']} - released={s['released']} "
          f"= leaked={s['leaked']} (balanced={s['balanced']})")
    print()
    print(t.report())
    for h in leaked:
        t.release(h)
    return s


def main():
    results = [
        test_all_released(),
        test_partial_leak(),
        test_exception_path(),
        test_double_release(),
        test_false_positive_exclusion(),
        test_context_manager(),
        test_accounting(),
    ]
    # 全部场景核算必须平账
    for s in results:
        assert s["balanced"], s
    print()
    print("全部自测通过。")


if __name__ == "__main__":
    main()
