"""selftest.py — 键集分页库自测。

覆盖：
  1. 首页 / 中间页 / 末页（含末页不满、page_size 整除与不整除）
  2. 随机对拍：键集全量遍历 == OFFSET 全量遍历 == 直接 ORDER BY 全表
  3. 翻页期间数据变化：插入(游标前/后)、删除(已翻/未翻)、更新排序键
  4. 退化情形：空表、单行、page_size 大于总行数

运行： python3 selftest.py
"""

import random
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keyset_page import KeysetPaginator, offset_page

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, score INTEGER NOT NULL)")
    conn.execute("CREATE INDEX idx_t_score ON t(score, id)")
    return conn


def insert_rows(conn, rows):
    conn.executemany("INSERT INTO t(id, score) VALUES (?, ?)", rows)
    conn.commit()


def keyset_all(conn, page_size):
    pag = KeysetPaginator(conn, "t", "score", "id", page_size)
    return [r for page in pag.iter_pages() for r in page.rows]


def offset_all(conn, page_size):
    out, page_no = [], 1
    while True:
        rows = offset_page(conn, "t", "score", "id", page_size, page_no)
        out.extend(rows)
        if len(rows) < page_size:
            return out
        page_no += 1


# ---------------------------------------------------------------- 1. 基本页
def test_first_middle_last():
    print("\n[1] 首页 / 中间页 / 末页")
    conn = fresh_conn()
    rows = [(i, (i * 7) % 13) for i in range(1, 1001)]   # 1000 行，大量重复键
    insert_rows(conn, rows)
    expected = sorted(rows, key=lambda r: (r[1], r[0]))

    for page_size in (1, 3, 100, 333, 1000, 5000):
        pag = KeysetPaginator(conn, "t", "score", "id", page_size)
        pages = list(pag.iter_pages())
        got = [r for p in pages for r in p.rows]
        check(f"page_size={page_size}: 全量一致", got == expected,
              f"got {len(got)} rows")
        check(f"page_size={page_size}: 首页正确",
              pages[0].rows == expected[:page_size])
        if len(pages) >= 3:
            mid = len(pages) // 2
            check(f"page_size={page_size}: 中间页(第{mid+1}页)正确",
                  pages[mid].rows == expected[mid*page_size:(mid+1)*page_size])
        check(f"page_size={page_size}: 末页正确",
              pages[-1].rows == expected[(len(pages)-1)*page_size:])
        check(f"page_size={page_size}: 末页 has_next=False",
              not pages[-1].has_next)
        check(f"page_size={page_size}: 无重复无遗漏",
              len(got) == len({r[0] for r in got}) == 1000)


# ---------------------------------------------------------------- 2. 随机对拍
def test_differential():
    print("\n[2] 随机对拍：键集 vs OFFSET vs ORDER BY 全表")
    rng = random.Random(20261006)
    for trial in range(30):
        conn = fresh_conn()
        n = rng.choice([0, 1, 2, 17, 100, 999, 1000, 3000])
        key_space = rng.choice([1, 2, 5, 50, 10**9])     # 含全部同键/大量重复
        rows = [(i, rng.randrange(key_space)) for i in range(1, n + 1)]
        rng.shuffle(rows)
        insert_rows(conn, rows)
        page_size = rng.choice([1, 2, 7, 64, 100, 1000])

        truth = sorted(rows, key=lambda r: (r[1], r[0]))
        ks = keyset_all(conn, page_size)
        off = offset_all(conn, page_size)
        ok = (ks == truth == off)
        check(f"trial{trial:02d} n={n} keys={key_space} ps={page_size}", ok,
              f"keyset={len(ks)} offset={len(off)} truth={len(truth)}")
        if not ok:
            return


# ------------------------------------------------------ 3. 翻页期间数据变化
def test_mutation_during_pagination():
    print("\n[3] 翻页期间数据变化（键集 vs OFFSET 对照）")
    conn = fresh_conn()
    insert_rows(conn, [(i, i) for i in range(1, 1001)])  # id=score=1..1000
    PS = 100

    pag = KeysetPaginator(conn, "t", "score", "id", PS)
    emitted = []

    # 第 1 页
    p = pag.first_page()
    emitted += [r[0] for r in p.rows]                    # 1..100

    # --- 翻页间隙发生的变化 ---
    # a) 游标之前插入 10 行（OFFSET 会因此重复输出）
    insert_rows(conn, [(10000 + i, 50 + i) for i in range(10)])   # score 50..59
    # b) 游标之后插入 5 行（应被后续页恰好输出一次）
    insert_rows(conn, [(20000 + i, 150 + i) for i in range(5)])   # score 150..154
    # c) 删除尚未翻到的行 id=250（OFFSET 会因此跳过一行未输出的数据）
    conn.execute("DELETE FROM t WHERE id = 250")
    # d) 删除已输出的行 id=30（不应有任何影响）
    conn.execute("DELETE FROM t WHERE id = 30")
    conn.commit()

    # 继续翻到末页
    while p.has_next:
        p = pag.next_page(p.cursor, p.page_no + 1)
        emitted += [r[0] for r in p.rows]

    # 期望序列 = 已输出的第 1 页 + 变化后库中所有 (score,id) > 游标(100,100) 的行
    final_rows = conn.execute("SELECT id, score FROM t").fetchall()
    rest = sorted((r for r in final_rows if (r[1], r[0]) > (100, 100)),
                  key=lambda r: (r[1], r[0]))
    expected = [i for i in range(1, 101)] + [r[0] for r in rest]
    check("无重复", len(emitted) == len(set(emitted)),
          f"dup={len(emitted)-len(set(emitted))}")
    check("结果集合与顺序符合预期（不重复不跳过）", emitted == expected,
          f"got {len(emitted)}, want {len(expected)}")
    check("游标前插入的行未被重复输出",
          not any(10000 <= i < 20000 for i in emitted))
    check("游标后插入的行恰好出现一次",
          sorted(i for i in emitted if i >= 20000) == [20000 + i for i in range(5)])
    check("未输出就被删除的行不出现", 250 not in emitted)

    # 对照：同样的变化下 OFFSET 分页
    conn2 = fresh_conn()
    insert_rows(conn2, [(i, i) for i in range(1, 1001)])
    off = [r[0] for r in offset_page(conn2, "t", "score", "id", PS, 1)]
    insert_rows(conn2, [(10000 + i, 50 + i) for i in range(10)])
    insert_rows(conn2, [(20000 + i, 150 + i) for i in range(5)])
    conn2.execute("DELETE FROM t WHERE id = 250")
    conn2.execute("DELETE FROM t WHERE id = 30")
    conn2.commit()
    page_no = 2
    while True:
        rows = offset_page(conn2, "t", "score", "id", PS, page_no)
        off += [r[0] for r in rows]
        if len(rows) < PS:
            break
        page_no += 1
    dup = len(off) - len(set(off))
    missing = len(set(expected) - set(off))
    print(f"        对照组 OFFSET：重复 {dup} 行，丢失 {missing} 行 "
          f"（键集：重复 0，丢失 0）")
    check("OFFSET 对照确实出现重复/丢失（证明场景有效）", dup > 0 or missing > 0)


def test_update_moves_row():
    print("\n[3b] UPDATE 把排序键从游标前改到游标后（固有语义说明）")
    conn = fresh_conn()
    insert_rows(conn, [(i, i) for i in range(1, 101)])
    pag = KeysetPaginator(conn, "t", "score", "id", 10)
    p = pag.first_page()                       # 1..10
    conn.execute("UPDATE t SET score = 55 WHERE id = 5")   # 已输出行移到游标后
    conn.execute("UPDATE t SET score = 3  WHERE id = 60")  # 未输出行移到游标前
    conn.commit()
    emitted = [r[0] for r in p.rows]
    while p.has_next:
        p = pag.next_page(p.cursor, p.page_no + 1)
        emitted += [r[0] for r in p.rows]
    check("前移的行会再次出现（键集固有问题，已文档化）", emitted.count(5) == 2)
    check("后移的行不再出现（视为已翻过）", 60 not in emitted)
    check("其余行无重复无遗漏",
          set(emitted) == (set(range(1, 101)) - {60}))


# ---------------------------------------------------------------- 4. 退化情形
def test_edge_cases():
    print("\n[4] 退化情形")
    conn = fresh_conn()
    pag = KeysetPaginator(conn, "t", "score", "id", 10)
    p = pag.first_page()
    check("空表：首页为空且无下一页", p.rows == [] and not p.has_next)

    insert_rows(conn, [(1, 42)])
    pages = list(KeysetPaginator(conn, "t", "score", "id", 10).iter_pages())
    check("单行：恰好一页", len(pages) == 1 and pages[0].rows == [(1, 42)])

    insert_rows(conn, [(i, i) for i in range(2, 6)])     # 共 5 行
    pages = list(KeysetPaginator(conn, "t", "score", "id", 100).iter_pages())
    check("page_size 大于总行数：一页取完", len(pages) == 1 and len(pages[0].rows) == 5)


if __name__ == "__main__":
    test_first_middle_last()
    test_differential()
    test_mutation_during_pagination()
    test_update_moves_row()
    test_edge_cases()
    print(f"\n==== 结果: {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)
