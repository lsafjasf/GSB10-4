#!/usr/bin/env python3
"""10 万会话绑定/校验的耗时与内存测量（仅标准库）。

运行: python3 bench_binding.py [会话数，默认 100000]
"""

import dataclasses
import gc
import os
import resource
import sys
import time
import tracemalloc
from datetime import datetime, timedelta, timezone

from cert_binding import BindingIdentity, SessionBindingStore, VerifyStatus

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    with open(os.path.join(FIXTURES, "alice.pem")) as fh:
        identity = BindingIdentity.from_pem(fh.read())
    session_ids = ["sess-%08d" % i for i in range(n)]

    # 每会话独立的指纹与有效期，模拟真实场景（字符串不共享，内存测量才真实）
    def make_identity(i: int) -> BindingIdentity:
        return dataclasses.replace(
            identity,
            subject_fp="%064x" % (i * 3),
            spki_fp="%064x" % (i * 3 + 1),
            cert_fp="%064x" % (i * 3 + 2),
            serial=i + 1,
            not_after=NOW + timedelta(days=i % 365 + 1),
        )

    store = SessionBindingStore()
    tracemalloc.start()
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    identities = [make_identity(i) for i in range(n)]
    t0 = time.perf_counter()
    for sid, ident in zip(session_ids, identities):
        r = store.verify(sid, ident, now=NOW)
        assert r.status is VerifyStatus.BOUND
    t_bind = time.perf_counter() - t0

    t0 = time.perf_counter()
    for sid, ident in zip(session_ids, identities):
        r = store.verify(sid, ident, now=NOW)
        assert r.status is VerifyStatus.OK
    t_verify = time.perf_counter() - t0

    _, peak = tracemalloc.get_traced_memory()
    # 释放压测侧身份对象后，current 即纯存储层占用
    del identities
    gc.collect()
    store_only, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # 换证书拒绝路径抽样（1 万次）
    with open(os.path.join(FIXTURES, "bob.pem")) as fh:
        other = BindingIdentity.from_pem(fh.read())
    t0 = time.perf_counter()
    for sid in session_ids[:10_000]:
        r = store.verify(sid, other, now=NOW)
        assert r.status is VerifyStatus.REJECT_CHANGED
    t_reject = time.perf_counter() - t0

    print("会话数:            %d" % n)
    print("首次绑定总耗时:    %.3f s  (%.2f us/次)" % (t_bind, t_bind / n * 1e6))
    print("重连校验总耗时:    %.3f s  (%.2f us/次)" % (t_verify, t_verify / n * 1e6))
    print("换证书拒绝(1万):   %.3f s  (%.2f us/次)" % (t_reject, t_reject / 10_000 * 1e6))
    print("tracemalloc 峰值:  %.1f MiB (含压测侧临时对象)" % (peak / 2**20))
    print("存储层实际占用:    %.1f MiB (%.0f B/会话)" % (store_only / 2**20, store_only / n))
    print("进程 RSS 增量:     %.1f MiB" % ((rss_after - rss_before) / 1024))


if __name__ == "__main__":
    main()
