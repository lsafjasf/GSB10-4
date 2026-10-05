"""
report.py — 生成真值表、对照数据与空值场景结果，输出到 data/ 目录。

运行：python3 report.py
"""

import os
import sqlite3

from nullsem.tvl import (
    AND, IS_NOT_NULL, IS_NULL, NOT, NULL, T, F, U,
    Col, Const, compare, eq, t_and, t_not, t_or,
)
from nullsem import engine

DATA = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA, exist_ok=True)

LABEL = {T: "T", F: "F", U: "U"}
SQLITE = {1: "T", 0: "F", None: "U"}


def w(name, text):
    path = os.path.join(DATA, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"写出 {path}")


def truth_tables():
    L = []
    L.append("三值逻辑真值表（T=TRUE, F=FALSE, U=UNKNOWN）\n")
    L.append("NOT:\n")
    L.append("  a | NOT a\n  ---+------")
    for a in (T, F, U):
        L.append(f"  {LABEL[a]} | {LABEL[t_not(a)]}")
    L.append("\nAND（FALSE 主导）:\n")
    L.append("  a b | a AND b\n  ----+--------")
    for a in (T, F, U):
        for b in (T, F, U):
            L.append(f"  {LABEL[a]} {LABEL[b]} | {LABEL[t_and(a, b)]}")
    L.append("\nOR（TRUE 主导）:\n")
    L.append("  a b | a OR b\n  ----+-------")
    for a in (T, F, U):
        for b in (T, F, U):
            L.append(f"  {LABEL[a]} {LABEL[b]} | {LABEL[t_or(a, b)]}")
    L.append("\n关键性质：")
    L.append("  NOT U = U      （UNKNOWN 不是 FALSE，双重否定不会凭空变真）")
    L.append("  U AND F = F    （FALSE 决定）")
    L.append("  U OR  T = T    （TRUE 决定）")
    L.append("  U AND U = U    （依然未知）")
    L.append("  U OR  F = U")
    return "\n".join(L)


def comparison_data():
    con = sqlite3.connect(":memory:")
    cur = con.cursor()
    cases = [
        ("NULL = NULL", NULL, "=", NULL),
        ("NULL <> NULL", NULL, "<>", NULL),
        ("NULL = 1", NULL, "=", 1),
        ("1 = NULL", 1, "=", NULL),
        ("NULL < 1", NULL, "<", 1),
        ("NULL <> 'x'", NULL, "<>", "x"),
        ("1 = 1", 1, "=", 1),
        ("1 = 2", 1, "=", 2),
        ("1 < 2", 1, "<", 2),
    ]
    L = ["空值比较：本库 vs 参考实现 SQLite（逐例对照）",
         "",
         f"{'表达式':<14}{'本库':<10}{'SQLite':<10}{'一致'}",
             "-" * 42]
    allok = True
    for text, a, op, b in cases:
        lib = LABEL[compare(op, a, b)]
        sql_a = "NULL" if a is NULL else (f"'{a}'" if isinstance(a, str) else str(a))
        sql_b = "NULL" if b is NULL else (f"'{b}'" if isinstance(b, str) else str(b))
        ref = SQLITE[cur.execute(f"SELECT {sql_a} {op} {sql_b}").fetchone()[0]]
        ok = lib == ref
        allok &= ok
        L.append(f"{text:<14}{lib:<10}{ref:<10}{'OK' if ok else 'DIFF'}")
    L.append("")
    L.append(f"结论：{'全部一致' if allok else '存在差异'}")
    L.append("")
    L.append("补充：IS NULL / IS NOT NULL 永不返回 UNKNOWN")
    L.append(f"{'NULL IS NULL':<14}")
    L.append(f"  本库={LABEL[IS_NULL(Const(NULL)).eval({})]}  "
             f"SQLite={SQLITE[cur.execute('SELECT NULL IS NULL').fetchone()[0]]}")
    return "\n".join(L)


def placement_scenarios():
    L = ["空值在分组 / 去重 / 排序中的位置（可复算）", ""]

    # GROUP BY
    rows = [{"g": 1, "v": 10}, {"g": None, "v": 20}, {"g": NULL, "v": 30},
            {"g": 1, "v": 40}, {"g": None, "v": 50}]
    L.append("1) GROUP BY g（输入 5 行，g 取值 1, NULL, NULL, 1, NULL）")
    out = engine.group_by(
        rows, ["g"],
        {"count(*)": engine.agg_count_star,
         "sum(v)": lambda rs: engine.agg_sum(rs, "v")})
    L.append("   结果：3 个 NULL 键落入同一组")
    L.append(f"   {'g':<8}{'count(*)':<10}{'sum(v)'}")
    for r in out:
        g = "NULL" if r["g"] is NULL else str(r["g"])
        s = "NULL" if r["sum(v)"] is NULL else str(r["sum(v)"])
        L.append(f"   {g:<8}{r['count(*)']:<10}{s}")

    # DISTINCT
    L.append("")
    L.append("2) DISTINCT x（输入序列：NULL, NULL, 1, 1, NULL, 2）")
    rows2 = [{"x": v} for v in (NULL, None, 1, 1, NULL, 2)]
    d = engine.distinct(rows2, ["x"])
    seq = ["NULL" if is_null(r["x"]) else str(r["x"]) for r in d]
    L.append(f"   结果（{len(d)} 行）: {', '.join(seq)}   <- 所有 NULL 合并为一个")

    # ORDER BY
    L.append("")
    L.append("3) ORDER BY x（输入：3, NULL, 1, NULL, 2，NULL 以两个独立行出现）")
    rows3 = [{"id": i, "x": v} for i, v in enumerate((3, NULL, 1, None, 2), 1)]
    for asc, nf, tag in ((True, False, "ASC  NULLS LAST"),
                         (True, True, "ASC  NULLS FIRST"),
                         (False, False, "DESC NULLS LAST"),
                         (False, True, "DESC NULLS FIRST")):
        out3 = engine.order_by(rows3, "x", ascending=asc, nulls_first=nf)
        seq = ["NULL" if is_null(r["x"]) else str(r["x"]) for r in out3]
        L.append(f"   {tag:<18}: {', '.join(seq)}")
    L.append("   （组内稳定：同值按输入 id 先后，两个 NULL 的相对顺序保持 2 在 4 前）")
    return "\n".join(L)


def is_null(v):
    return v is NULL or v is None


def propagation_scenarios():
    L = ["UNKNOWN 在过滤 / 连接 / 聚合中的传递（逐一验证）", ""]
    rows = [{"id": 1, "a": 10}, {"id": 2, "a": NULL}, {"id": 3, "a": 30}]
    L.append("WHERE a > 15:")
    L.append("  id=1 -> FALSE 丢弃；id=2 -> UNKNOWN 丢弃；id=3 -> TRUE 保留")
    L.append(f"  本库保留: {[r['id'] for r in engine.where(rows, gt(Col('a'), Const(15)))]}")
    L.append("WHERE NOT (a > 15):")
    L.append("  id=1 -> TRUE 保留；id=2 -> NOT UNKNOWN = UNKNOWN 丢弃；id=3 -> FALSE 丢弃")
    L.append(f"  本库保留: {[r['id'] for r in engine.where(rows, NOT(gt(Col('a'), Const(15))))]}")

    L.append("")
    L.append("JOIN ON l.k = r.k（两表 k 各为 1, NULL）")
    left = [{"k": 1}, {"k": NULL}]
    right = [{"k": 1}, {"k": NULL}]
    j = engine.inner_join(left, right, eq(Col("l_k"), Col("r_k")), lprefix="l_", rprefix="r_")
    L.append(f"  配对数: {len(j)}（NULL=NULL 为 UNKNOWN，不配对；只保留 1=1）")

    L.append("")
    L.append("聚合：COUNT(*) 不看 NULL；COUNT(a)/SUM/AVG/MIN/MAX 跳过 NULL；")
    L.append("      全 NULL 列时 SUM/AVG/MIN/MAX = NULL，COUNT = 0")
    return "\n".join(L)


def gt(a, b):
    from nullsem.tvl import Cmp
    return Cmp(">", a, b)


if __name__ == "__main__":
    w("truth_tables.txt", truth_tables() + "\n")
    w("comparison_data.txt", comparison_data() + "\n")
    w("placement_scenarios.txt", placement_scenarios() + "\n")
    w("propagation_scenarios.txt", propagation_scenarios() + "\n")
