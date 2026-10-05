"""10 万会话绑定校验基准：耗时与内存上界。

运行: python3 bench_binding.py [n]
"""

import gc
import sys
import time
import tracemalloc

from cert_session_binding import PeerCertificate, SessionBinder, Verdict


def main(n: int = 100_000) -> None:
    cert = PeerCertificate("CN=bench,O=Example", b"\xaa" * 294, 9_999_999_999.0, 1)
    binder = SessionBinder(now=lambda: 1_000.0)

    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()

    # 阶段1: 10 万次首次连接（建立绑定）
    for i in range(n):
        d = binder.validate(f"sess-{i}", cert)
        assert d.verdict is Verdict.ACCEPT_NEW
    t1 = time.perf_counter()

    # 阶段2: 10 万次正常重连（绑定一致）
    for i in range(n):
        d = binder.validate(f"sess-{i}", cert)
        assert d.verdict is Verdict.ACCEPT_RESUME
    t2 = time.perf_counter()

    # 阶段3: 10 万次换证书重连（拒绝 + 作废）
    bad = PeerCertificate("CN=bench,O=Example", b"\xbb" * 294, 9_999_999_999.0, 2)
    for i in range(n):
        d = binder.validate(f"sess-{i}", bad)
        assert d.verdict is Verdict.REJECT_CERT_CHANGED
        assert d.session_invalidated
    t3 = time.perf_counter()

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 内存上界：阶段3 前会话表满载(n 条)时重新测一次峰值
    gc.collect()
    tracemalloc.start()
    binder2 = SessionBinder(now=lambda: 1_000.0)
    for i in range(n):
        binder2.validate(f"sess-{i}", cert)
    _, peak_full = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"会话数 n                = {n:,}")
    print(f"阶段1 首次连接建立绑定   : {t1 - t0:.3f}s  ({n / (t1 - t0):,.0f} ops/s)")
    print(f"阶段2 正常重连校验       : {t2 - t1:.3f}s  ({n / (t2 - t1):,.0f} ops/s)")
    print(f"阶段3 换证书拒绝+作废    : {t3 - t2:.3f}s  ({n / (t3 - t2):,.0f} ops/s)")
    print(f"三阶段合计               : {t3 - t0:.3f}s")
    print(f"满载 {n:,} 会话内存峰值   : {peak_full / 1024 / 1024:.1f} MiB"
          f"  (约 {peak_full / n:.0f} 字节/会话)")
    print(f"内存上界估算             : O(n)，每会话 1 条 dict 记录"
          f"(32B 绑定标识 + 主体DN + 公钥指纹 + 过期时间)，实测约 {peak_full / n:.0f} B/会话")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 100_000)
