#!/usr/bin/env python3
"""深度翻页自测：正确性对拍 / 耗时曲线 / 翻页中数据变化边界 / 1000 万全量。

用法：
    python3 selftest.py           # 快速场景（A 对拍、B 曲线 100 万、C 边界）
    python3 selftest.py --full    # 追加场景 D：1000 万条翻到最后一页
    python3 selftest.py --full --keep   # 保留生成的数据库文件
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

from deeppage import (KeysetPager, OffsetPager, connect, create_index,
                      explain_keyset_plan, init_schema, seed)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bench_data")

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def fresh_db(name: str):
    path = os.path.join(DATA_DIR, name)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    conn = connect(path)
    init_schema(conn)
    return conn, path


def bar(value: float, vmax: float, width: int = 40) -> str:
    n = 1 if value > 0 else 0
    n = max(n, round(value / vmax * width)) if vmax > 0 else 0
    return "█" * min(n, width)


# ---------------------------------------------------------------- 场景 A：对拍
def scenario_correctness() -> None:
    print("\n=== 场景 A：键集分页 vs 全量排序 / OFFSET 逐页对拍（5 万行，k 大量重复）===")
    conn, _ = fresh_db("small.db")
    n, k_mod, page = 50_000, 97, 100
    seed(conn, n, k_mod=k_mod)
    create_index(conn)

    truth = conn.execute(
        "SELECT id, k, payload FROM items ORDER BY k, id;").fetchall()

    pager = KeysetPager(conn, page)
    got = []
    while True:
        rows, _ = pager.next_page()
        if not rows:
            break
        got.extend(rows)
    check("A1 键集遍历 == 全量 ORDER BY k,id（对拍）", got == truth,
          f"{len(got)} 行 / {n} 行")

    op, kp = OffsetPager(conn, page), KeysetPager(conn, page)
    mismatch, pages, samples = 0, 0, []
    while True:
        orow, _ = op.next_page()
        krow, _ = kp.next_page()
        pages += 1
        if orow != krow:
            mismatch += 1
        if pages in (1, 250, 500) and krow:
            samples.append((pages, (krow[0][1], krow[0][0]),
                            (krow[-1][1], krow[-1][0]), orow == krow))
        if not orow and not krow:
            break
    check("A2 键集 vs OFFSET 逐页一致", mismatch == 0, f"{pages - 1} 页全部一致")
    print("  对拍抽样（页号, 首页键(k,id), 末行键(k,id), 与OFFSET一致）:")
    for s in samples:
        print(f"    page {s[0]:>4}: first={s[1]} last={s[2]} match={s[3]}")

    plan = explain_keyset_plan(conn)
    print("  键集查询计划：", plan.replace("\n", " ; "))
    check("A3 键集查询走 (k,id) 索引范围扫描",
          "SEARCH" in plan and "idx_items_k_id" in plan)
    conn.close()


# ---------------------------------------------------------------- 场景 B：曲线
def scenario_curve() -> None:
    print("\n=== 场景 B：单页耗时 vs 页号（100 万行，page=1000，共 1000 页）===")
    conn, _ = fresh_db("curve.db")
    n, page = 1_000_000, 1000
    seed(conn, n)
    create_index(conn)

    sample_pages = [1, 10, 100, 250, 500, 750, 1000]
    keyset_t, offset_t = {}, {}

    pager = KeysetPager(conn, page)
    while pager.page_no < max(sample_pages):
        rows, dt = pager.next_page()
        if pager.page_no in sample_pages:
            keyset_t[pager.page_no] = dt
        if not rows:
            break

    pager = OffsetPager(conn, page)
    while pager.page_no < max(sample_pages):
        rows, dt = pager.next_page()
        if pager.page_no in sample_pages:
            offset_t[pager.page_no] = dt
        if not rows:
            break

    vmax = max(offset_t.values())
    print(f"  {'页号':>6} {'键集(ms)':>10} {'OFFSET(ms)':>11}  OFFSET 相对耗时")
    for p in sample_pages:
        k, o = keyset_t[p] * 1e3, offset_t[p] * 1e3
        print(f"  {p:>6} {k:>10.3f} {o:>11.3f}  |{bar(o, vmax)}")
    t_first, t_last = keyset_t[1] * 1e3, keyset_t[max(sample_pages)] * 1e3
    check("B1 键集单页耗时不随页号增长",
          t_last < max(10 * t_first, t_first + 5),
          f"第 1 页 {t_first:.3f}ms vs 第 1000 页 {t_last:.3f}ms")
    check("B2 OFFSET 明显越翻越慢（对照组）",
          offset_t[1000] > 5 * offset_t[1],
          f"第 1 页 {offset_t[1]*1e3:.3f}ms -> 第 1000 页 {offset_t[1000]*1e3:.3f}ms")
    conn.close()


# ------------------------------------------------------- 场景 C：翻页中数据变化
def scenario_churn() -> None:
    print("\n=== 场景 C：翻页期间并发增删（2 万行，page=100，每 3 页扰动一次）===")
    n, page = 20_000, 100

    def churn(writer, deleted_ids: list[int]) -> None:
        # 每次在游标之前删除 2 行、插入 1 行，在游标之后插入 1 行：
        # 对 OFFSET 而言游标前净减 1 行，后续行会错位被跳过（制造可复现的遗漏）；
        # 对键集游标而言游标之前的增删完全不影响，游标之后的新增按序出现。
        for _ in range(2):
            row = writer.execute(
                "SELECT id FROM items ORDER BY k, id LIMIT 1;").fetchone()
            writer.execute("DELETE FROM items WHERE id = ?;", (row[0],))
            deleted_ids.append(row[0])
        writer.execute("INSERT INTO items (k, payload) VALUES (-1, 'churn-head');")
        writer.execute("INSERT INTO items (k, payload) VALUES (1000000000, 'churn-tail');")

    def run_with_churn(pager_factory):
        conn, _ = fresh_db("churn.db")
        seed(conn, n)
        create_index(conn)
        baseline = conn.execute(
            "SELECT id, k, payload FROM items ORDER BY k, id;").fetchall()
        writer = connect(conn.execute("PRAGMA database_list;").fetchone()[2])
        pager = pager_factory(conn)
        got, i, deleted_ids = [], 0, []
        while True:
            rows, _ = pager.next_page()
            if not rows:
                break
            got.extend(rows)
            i += 1
            if i % 3 == 0:
                churn(writer, deleted_ids)
        if hasattr(pager, "close"):
            pager.close()
        writer.close()
        conn.close()
        return baseline, got, deleted_ids

    # C1：快照模式 —— 结果必须与翻页前快照完全一致
    baseline, got, _ = run_with_churn(lambda c: KeysetPager(c, page, snapshot=True))
    check("C1 快照翻页：并发增删下结果与翻页前快照完全一致（不重复不跳过）",
          got == baseline, f"{len(got)} 行 == 快照 {len(baseline)} 行")

    # C2-C4：实时模式 —— 不重复、单调、稳定行不遗漏
    baseline, got, deleted = run_with_churn(lambda c: KeysetPager(c, page, snapshot=False))
    ids = [r[0] for r in got]
    keys = [(r[1], r[0]) for r in got]
    check("C2 实时键集翻页：无重复行", len(ids) == len(set(ids)),
          f"{len(ids)} 行, 重复 {len(ids) - len(set(ids))}")
    check("C3 实时键集翻页：(k,id) 严格递增无回退",
          all(keys[i] < keys[i + 1] for i in range(len(keys) - 1)))
    stable_ids = {r[0] for r in baseline} - set(deleted)
    check("C4 实时键集翻页：未被删除的旧行无一遗漏",
          stable_ids <= set(ids),
          f"稳定行 {len(stable_ids)} 全部出现: {stable_ids <= set(ids)}")

    # C5：OFFSET 对照组在同样扰动下必然重复/遗漏
    baseline, got, deleted = run_with_churn(lambda c: OffsetPager(c, page))
    ids = [r[0] for r in got]
    dup = len(ids) - len(set(ids))
    missing = len(({r[0] for r in baseline} - set(deleted)) - set(ids))
    print(f"  OFFSET 对照组：重复 {dup} 行，遗漏 {missing} 行")
    check("C5 OFFSET 在同样扰动下出现重复/遗漏（对照组，证明问题存在）",
          dup > 0 or missing > 0, f"dup={dup} missing={missing}")


# ------------------------------------------------- 场景 D：1000 万翻到最后一页
def scenario_full() -> None:
    print("\n=== 场景 D：1000 万行翻到最后一页（page=1000，共 10000 页）===")
    conn, path = fresh_db("full.db")
    n, page = 10_000_000, 1000
    t0 = time.perf_counter()
    seed(conn, n)
    create_index(conn)
    print(f"  建库+索引耗时 {time.perf_counter() - t0:.1f}s, "
          f"文件 {os.path.getsize(path) / 1e6:.0f} MB")

    cnt = conn.execute("SELECT COUNT(*) FROM items;").fetchone()[0]
    check("D0 行数", cnt == n, f"{cnt}")

    pager = KeysetPager(conn, page)
    times, total_rows = [], 0
    cursor_at_5000 = None
    t0 = time.perf_counter()
    while True:
        rows, dt = pager.next_page()
        if not rows:
            break
        times.append(dt)
        total_rows += len(rows)
        if pager.page_no == 5000:
            cursor_at_5000 = pager.cursor
    total = time.perf_counter() - t0

    times_sorted = sorted(times)
    p50 = times_sorted[len(times) // 2] * 1e3
    p99 = times_sorted[int(len(times) * 0.99)] * 1e3
    tmax = max(times) * 1e3
    check("D1 键集遍历行数 == 1000 万", total_rows == n, f"{total_rows}")
    print(f"  总耗时 {total:.2f}s | 单页 avg {total / len(times) * 1e3:.3f}ms "
          f"p50 {p50:.3f}ms p99 {p99:.3f}ms max {tmax:.3f}ms")
    print("  抽样页耗时：")
    vmax = max(times) * 1e3
    for p in [1, 1000, 2000, 4000, 6000, 8000, 9999, 10000]:
        t = times[p - 1] * 1e3
        print(f"    page {p:>5}: {t:>7.3f}ms |{bar(t, vmax)}")
    check("D2 单页耗时上界 < 100ms（含最后一页）", tmax < 100, f"max {tmax:.3f}ms")

    # 从第 5000 页游标直接续翻一页：证明任意位置恢复都是 O(page_size)
    k, i = cursor_at_5000
    t0 = time.perf_counter()
    conn.execute(
        "SELECT id, k, payload FROM items "
        "WHERE (k, id) > (?, ?) ORDER BY k, id LIMIT ?;",
        (k, i, page)).fetchall()
    resume_ms = (time.perf_counter() - t0) * 1e3
    print(f"  从第 5000 页游标续翻一页：{resume_ms:.3f}ms")
    check("D3 游标续翻单页 < 100ms", resume_ms < 100, f"{resume_ms:.3f}ms")

    # OFFSET 对照：只测 3 个深度（全量 OFFSET 需数 100 亿行，不现实）
    print("  OFFSET 对照（单页，含从头数行的代价）：")
    prev = None
    ok_growth = True
    for off in (0, 5_000_000, 9_999_000):
        t0 = time.perf_counter()
        conn.execute(
            "SELECT id, k, payload FROM items ORDER BY k, id LIMIT ? OFFSET ?;",
            (page, off)).fetchall()
        dt = (time.perf_counter() - t0) * 1e3
        print(f"    OFFSET {off:>9}: {dt:>9.1f}ms")
        if prev is not None and dt <= prev:
            ok_growth = False
        prev = dt
    check("D4 OFFSET 耗时随页号单调增长（对照组）", ok_growth)
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="追加 1000 万行场景 D")
    ap.add_argument("--keep", action="store_true", help="保留数据库文件")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    t0 = time.perf_counter()
    scenario_correctness()
    scenario_curve()
    scenario_churn()
    if args.full:
        scenario_full()
    print(f"\n总耗时 {time.perf_counter() - t0:.1f}s")

    if not args.keep:
        shutil.rmtree(DATA_DIR, ignore_errors=True)

    if FAILURES:
        print(f"\n{len(FAILURES)} 项失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\n全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
