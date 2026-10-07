"""自测：切分规则、边界用例、断点续跑、重试分类、对拍、基准。
运行：python3 -m sqlchunk.selftest
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
import tempfile
import time

from .splitter import split_statements, ScriptSyntaxError
from .chunker import build_chunks
from .checkpoint import Checkpoint, CheckpointCorruptedError
from .runner import Runner, RetryableError, FatalError

HERE = os.path.dirname(os.path.abspath(__file__))
TESTDATA = os.path.join(HERE, "testdata")
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


# ---------- 1. 切分规则 ----------
def test_splitter_rules():
    print("[1] 切分规则")
    script = (
        "INSERT INTO t VALUES ('a;b', \"c;d\", `e;f`);\n"      # 字符串内分号
        "-- comment with ; and 'quote'\n"
        "# sharp comment ;\n"
        "/* block ; comment ; */ INSERT INTO t VALUES ('it''s');\n"  # 双写转义
        "INSERT INTO t VALUES ('back\\\\slash;');\n"           # 反斜杠转义
        "UPDATE t SET x = 1 \\\n+ 2 WHERE id = 3;\n"           # 续行
        ";;\n"                                                  # 空语句丢弃
        "DELETE FROM t WHERE note = '--not-comment';\n"
    )
    stmts = [s for _, s in split_statements(script)]
    check("语句数=5", len(stmts) == 5, f"got {len(stmts)}")
    check("字符串内分号保留", "'a;b', \"c;d\", `e;f`" in stmts[0])
    check("块注释后语句完整", "/* block ; comment ; */ INSERT" in stmts[1])
    check("双写引号", "it''s" in stmts[1])
    check("续行不断句", "\\\n+ 2" in stmts[3])
    check("行注释内分号不切", all("-- comment" not in s.split("VALUES")[-1] for s in stmts))

    try:
        split_statements("INSERT INTO t VALUES ('unterminated);")
        check("未闭合字符串报错", False)
    except ScriptSyntaxError as e:
        check("未闭合字符串报错", e.offset >= 0)
    try:
        split_statements("/* never closed")
        check("未闭合块注释报错", False)
    except ScriptSyntaxError:
        check("未闭合块注释报错", True)


# ---------- 2. 分块边界 ----------
def test_chunking():
    print("[2] 分块边界（单块 / 超大块）")
    small = "SELECT 1;\nSELECT 2;\n"
    chunks = build_chunks(small, max_chunk_bytes=1024)
    check("单块", len(chunks) == 1 and chunks[0].stmt_count == 2)

    big_stmt = "INSERT INTO t VALUES ('" + "x" * 5000 + "')"
    script = "SELECT 1;\n" + big_stmt + ";\nSELECT 2;\n"
    chunks = build_chunks(script, max_chunk_bytes=1024)
    check("超大语句独占一块", len(chunks) == 3 and chunks[1].stmt_count == 1
          and chunks[1].byte_size > 1024)
    check("普通块不超限", all(c.byte_size <= 1024 for c in (chunks[0], chunks[2])))

    # 任意脚本：块拼接回语句序列恒等
    joined = [s for c in chunks for s in c.statements]
    origin = [s for _, s in split_statements(script)]
    check("分块不丢不改语句", joined == origin)


# ---------- 3. 断点续跑 & 副作用不重放 ----------
class SQLiteExecutor:
    """把每块逐条打进 sqlite（事务），并把执行日志写进 side_effect_log。"""

    def __init__(self, db_path, log, fail_plan=None):
        self.conn = sqlite3.connect(db_path)
        self.log = log
        self.fail_plan = fail_plan or {}  # chunk_idx -> [RetryableError x N, ...]
        self.calls = []

    def __call__(self, chunk_text, chunk_idx):
        self.calls.append(chunk_idx)
        if self.fail_plan.get(chunk_idx):
            err = self.fail_plan[chunk_idx].pop(0)
            if err == "retry":
                raise RetryableError(f"chunk {chunk_idx} locked")
            raise FatalError(f"chunk {chunk_idx} syntax error")
        stmts = [s for _, s in split_statements(chunk_text)]
        with self.conn:  # 事务：块内要么全成要么全败
            for s in stmts:
                self.conn.execute(s)
                self.log.append((chunk_idx, s))


def test_resume():
    print("[3] 断点续跑 / 失败分类 / 副作用不重放")
    tmp = tempfile.mkdtemp()
    script = "".join(
        f"CREATE TABLE t{i}(v INTEGER);INSERT INTO t{i} VALUES ({i});\n"
        for i in range(12)
    )
    ckpt = os.path.join(tmp, "ckpt.json")
    db = os.path.join(tmp, "x.db")

    # 3a. 第 2 块先可重试失败 2 次后成功；第 4 块致命失败
    log = []
    ex = SQLiteExecutor(db, log, fail_plan={2: ["retry", "retry"], 4: ["fatal"]})
    r = Runner(script, ckpt, ex, max_chunk_bytes=120, max_retries=3, retry_backoff=0)
    rep = r.run()
    check("致命失败终止", rep.status == "failed_fatal" and rep.failed_chunk == 4)
    recs = r.checkpoint.records()
    check("可重试块重试3次后done", recs[2]["status"] == "done" and recs[2]["attempts"] == 3,
          f"attempts={recs[2]['attempts']}")
    check("致命块attempts=1", recs[4]["status"] == "failed_fatal" and recs[4]["attempts"] == 1)
    done_before = sum(1 for x in recs if x["status"] == "done")
    calls_before = list(ex.calls)

    # 3b. 修复后重启：从第 4 块继续，前 4 块不得重放
    log2 = []
    ex2 = SQLiteExecutor(db, log2)
    r2 = Runner(script, ckpt, ex2, max_chunk_bytes=120, retry_backoff=0)
    rep2 = r2.run()
    check("续跑完成", rep2.status == "done")
    check("从断点继续", min(ex2.calls) == 4, f"calls={ex2.calls}")
    check("已执行块零重放", all(c >= 4 for c in ex2.calls))
    check("副作用总数=语句总数", len(log) + len(log2) == 24,
          f"{len(log)}+{len(log2)}")
    check("续跑前已完成块数", done_before == 4)

    # 3c. 可重试失败耗尽重试
    ckpt3 = os.path.join(tmp, "ckpt3.json")
    db3 = os.path.join(tmp, "y.db")
    ex3 = SQLiteExecutor(db3, [], fail_plan={1: ["retry"] * 10})
    r3 = Runner(script, ckpt3, ex3, max_chunk_bytes=120, max_retries=2, retry_backoff=0)
    rep3 = r3.run()
    rec3 = r3.checkpoint.records()[1]
    check("重试耗尽标记failed_retryable",
          rep3.status == "failed_retryable" and rec3["attempts"] == 3
          and rec3["status"] == "failed_retryable")


# ---------- 4. 断点文件损坏 ----------
def test_corrupted_checkpoint():
    print("[4] 断点文件损坏")
    tmp = tempfile.mkdtemp()
    script = "CREATE TABLE a(v);\nINSERT INTO a VALUES (1);\n"
    ckpt = os.path.join(tmp, "c.json")
    Runner(script, ckpt, lambda t, i: None, max_chunk_bytes=4096).run()

    # 截断
    with open(ckpt) as f:
        good = f.read()
    with open(ckpt, "w") as f:
        f.write(good[: len(good) // 2])
    try:
        Runner(script, ckpt, lambda t, i: None)
        check("截断的断点被拒绝", False)
    except CheckpointCorruptedError:
        check("截断的断点被拒绝", True)

    # 脚本被改动（哈希不匹配）
    os.remove(ckpt)  # 损坏后安全处理：人工确认后删除重建，库本身拒绝自动覆盖
    Runner(script, ckpt, lambda t, i: None).run()
    try:
        Runner(script + "SELECT 9;\n", ckpt, lambda t, i: None)
        check("脚本变更被拒绝", False)
    except CheckpointCorruptedError:
        check("脚本变更被拒绝", True)

    # 篡改块状态
    os.remove(ckpt)
    Runner(script, ckpt, lambda t, i: None).run()
    import json
    with open(ckpt) as f:
        d = json.load(f)
    d["chunks"][0]["status"] = "hacked"
    with open(ckpt, "w") as f:
        json.dump(d, f)
    try:
        Runner(script, ckpt, lambda t, i: None)
        check("篡改状态被拒绝", False)
    except CheckpointCorruptedError:
        check("篡改状态被拒绝", True)


# ---------- 5. 对拍：分块执行 vs 一次性执行 ----------
def test_differential():
    print("[5] 对拍（分块 vs 一次性）")
    rng = random.Random(20261007)
    os.makedirs(TESTDATA, exist_ok=True)
    path = os.path.join(TESTDATA, "diff_script.sql")
    lines = ["CREATE TABLE kv(k TEXT, v INTEGER, note TEXT);"]
    for i in range(2000):
        note = rng.choice(["plain", "has;semicolon", "it's", "--dash", "/*c*/", "续行\\\n符"])
        note_sql = note.replace("'", "''")
        lines.append(f"INSERT INTO kv VALUES('k{i}', {rng.randint(-10**6, 10**6)}, '{note_sql}');")
    script = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)

    def run_all(mode):
        db = os.path.join(TESTDATA, f"diff_{mode}.db")
        if os.path.exists(db):
            os.remove(db)
        conn = sqlite3.connect(db)
        if mode == "oneshot":
            for _, s in split_statements(script):
                conn.execute(s)
            conn.commit()
        else:
            log = []
            ex = SQLiteExecutor(db, log)
            ckpt = os.path.join(TESTDATA, "diff_ckpt.json")
            if os.path.exists(ckpt):
                os.remove(ckpt)
            rep = Runner(script, ckpt, ex, max_chunk_bytes=4096, retry_backoff=0).run()
            assert rep.status == "done"
        dump = "\n".join(conn.iterdump())
        conn.close()
        return dump

    a, b = run_all("oneshot"), run_all("chunked")
    with open(os.path.join(TESTDATA, "diff_oneshot.txt"), "w") as f:
        f.write(a)
    with open(os.path.join(TESTDATA, "diff_chunked.txt"), "w") as f:
        f.write(b)
    check("对拍结果逐字节一致", a == b)


# ---------- 6. 基准：10 万条语句 ----------
def test_benchmark():
    print("[6] 基准：100,000 条语句")
    script = "".join(
        f"INSERT INTO bench VALUES({i},'v{i:06d}','note;with--stuff');\n"
        for i in range(100_000)
    )
    print(f"  脚本大小: {len(script)/1e6:.2f} MB")
    for budget in (64 << 10, 1 << 20):
        t0 = time.perf_counter()
        chunks = build_chunks(script, max_chunk_bytes=budget)
        dt = time.perf_counter() - t0
        tmp = tempfile.mkdtemp()
        ckpt = os.path.join(tmp, "bench.json")
        cp = Checkpoint(ckpt, script, chunks)
        cp.save()
        size = cp.byte_size()
        print(f"  max_chunk_bytes={budget:>8}: 分块耗时 {dt*1000:7.1f} ms, "
              f"块数 {len(chunks):>5}, 断点文件 {size} 字节 "
              f"(~{size/len(chunks):.0f} 字节/块)")
        check(f"分块耗时<5s (budget={budget})", dt < 5.0, f"{dt:.2f}s")


def main():
    test_splitter_rules()
    test_chunking()
    test_resume()
    test_corrupted_checkpoint()
    test_differential()
    test_benchmark()
    print(f"\n合计: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
