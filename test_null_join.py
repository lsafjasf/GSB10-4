"""自测：null_join 库与参考实现(sqlite3, SQL 三值逻辑)逐行对拍。

覆盖数据情形：
  1. no_null        两表完全没有空外键
  2. partial_null   两表部分键为空
  3. left_all_null  左表键整列为空
  4. both_all_null  两表键整列均为空
  5. mixed          空值/非空/重复键/多对多/独有键混合

对拍内容：行数、sha256(规范行)、逐行相等、不一致明细。
报告输出到 reports/diff_report.txt，完整结果落盘 reports/dumps/。
"""
import hashlib
import os
import random
import sqlite3
import unittest

import null_join as nj

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(HERE, "reports")
DUMP_DIR = os.path.join(REPORT_DIR, "dumps")
NULL = None


# ----------------------------- 数据集 -----------------------------

def make_datasets():
    rng = random.Random(20261005)

    def gen(n, null_ratio, key_domain, value_seed):
        rows = []
        for i in range(n):
            if rng.random() < null_ratio:
                k = NULL
            else:
                k = rng.randrange(key_domain)
            rows.append((k, f"{value_seed}{i}"))
        return rows

    no_null_l = [(i % 7, f"L{i}") for i in range(60)]            # 0..6，重复键
    no_null_r = [(i % 5, f"R{i}") for i in range(50)]            # 0..4，右表独有键 5,6 不会出现

    partial_l = gen(120, 0.25, 8, "L")
    partial_r = gen(100, 0.35, 8, "R")

    left_all_null_l = [(NULL, f"L{i}") for i in range(40)]
    left_all_null_r = gen(60, 0.2, 6, "R")

    both_all_null_l = [(NULL, f"L{i}") for i in range(20)]
    both_all_null_r = [(NULL, f"R{i}") for i in range(20)]

    mixed_l = []
    for i in range(300):
        r = rng.random()
        k = NULL if r < 0.2 else (rng.randrange(10) if r < 0.9 else 999)  # 999 独有键
        mixed_l.append((k, f"L{i}"))
    mixed_r = []
    for i in range(260):
        r = rng.random()
        k = NULL if r < 0.3 else rng.randrange(10)                          # 含重复键 => 多对多
        mixed_r.append((k, f"R{i}"))
    mixed_r.append((5, "Rextra0"))                                          # 右表补充一个重复键
    mixed_r.append((5, "Rextra1"))

    return {
        "no_null": (no_null_l, no_null_r),
        "partial_null": (partial_l, partial_r),
        "left_all_null": (left_all_null_l, left_all_null_r),
        "both_all_null": (both_all_null_l, both_all_null_r),
        "mixed": (mixed_l, mixed_r),
    }


# --------------------------- 参考实现(sqlite3) ---------------------------

JOIN_SQL = {
    # 列顺序与库输出 l+r 保持一致: (lk, lv, rk, rv)
    "inner": "SELECT L.k, L.v, R.k, R.v FROM L JOIN R ON L.k = R.k",
    "left":  "SELECT L.k, L.v, R.k, R.v FROM L LEFT JOIN R ON L.k = R.k",
    "right": "SELECT L.k, L.v, R.k, R.v FROM R LEFT JOIN L ON L.k = R.k",
    "full": (
        "SELECT L.k, L.v, R.k, R.v FROM L LEFT JOIN R ON L.k = R.k "
        "UNION ALL "
        "SELECT L.k, L.v, R.k, R.v FROM R LEFT JOIN L ON L.k = R.k "
        "WHERE L.k IS NULL"
    ),
    "semi": ("SELECT DISTINCT L.k, L.v FROM L WHERE EXISTS "
             "(SELECT 1 FROM R WHERE L.k = R.k)"),
    "anti": ("SELECT DISTINCT L.k, L.v FROM L WHERE NOT EXISTS "
             "(SELECT 1 FROM R WHERE L.k = R.k)"),
}


def reference_run(name, left, right):
    con = sqlite3.connect(":memory:")
    cur = con.cursor()
    cur.execute("CREATE TABLE L(k INTEGER, v TEXT)")
    cur.execute("CREATE TABLE R(k INTEGER, v TEXT)")
    cur.executemany("INSERT INTO L VALUES (?,?)", left)
    cur.executemany("INSERT INTO R VALUES (?,?)", right)
    if name == "right":
        sql = ("SELECT L.k, L.v, R.k, R.v FROM R LEFT JOIN L ON L.k = R.k")
    else:
        sql = JOIN_SQL[name]
    rows = cur.execute(sql).fetchall()
    con.close()
    return nj.sort_rows(rows)


# ----------------------------- 被测实现 -----------------------------

LIB_RUNNERS = {
    "inner": lambda l, r: nj.inner_join(l, r, 0, 0),
    "left":  lambda l, r: nj.left_join(l, r, 0, 0),
    "right": lambda l, r: nj.right_join(l, r, 0, 0),
    "full":  lambda l, r: nj.full_join(l, r, 0, 0),
    "semi":  lambda l, r: nj.semi_join(l, r, 0, 0),
    "anti":  lambda l, r: nj.anti_join(l, r, 0, 0),
}


def canonical_text(rows):
    lines = ["\t".join("NULL" if c is None else str(c) for c in row) for row in rows]
    return "\n".join(lines), hashlib.sha256("\n".join(lines).encode()).hexdigest()


class NullJoinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(DUMP_DIR, exist_ok=True)
        cls.datasets = make_datasets()
        cls.report = []
        cls.failures_detail = []

    @classmethod
    def tearDownClass(cls):
        path = os.path.join(REPORT_DIR, "diff_report.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(cls.report) + "\n")

    def _check(self, case, join_name):
        left, right = self.datasets[case]
        got = nj.sort_rows(LIB_RUNNERS[join_name](left, right))
        exp = reference_run(join_name, left, right)

        got_text, got_hash = canonical_text(got)
        exp_text, exp_hash = canonical_text(exp)

        dump = os.path.join(DUMP_DIR, f"{case}__{join_name}.tsv")
        with open(dump, "w", encoding="utf-8") as f:
            f.write("# 规范排序后的对拍结果（NULL 排末尾）\n")
            f.write("# 列: " + ("lk\tlv\trk\trv\n" if join_name in
                    ("inner", "left", "right", "full") else "lk\tlv\n"))
            f.write(got_text + "\n")

        mismatches = []
        if len(got) != len(exp):
            mismatches.append(f"行数不一致: 库={len(got)} 参考={len(exp)}")
        for i, (g, e) in enumerate(zip(got, exp)):
            if g != e:
                mismatches.append(f"第{i+1}行 库={g} 参考={e}")
                if len(mismatches) >= 5:
                    break
        if got_hash != exp_hash:
            mismatches.append(f"sha256 不一致: 库={got_hash} 参考={exp_hash}")

        line = (f"{case:14s} {join_name:5s} 行数 库={len(got):5d} 参考={len(exp):5d} "
                f"sha256={'一致' if got_hash == exp_hash else '不一致'}")
        self.report.append(line)
        for m in mismatches:
            self.report.append(f"    !! {m}")
            self.failures_detail.append((case, join_name, m))
        self.assertEqual(got_hash, exp_hash, f"{case}/{join_name} 对拍失败")
        self.assertEqual(got, exp, f"{case}/{join_name} 逐行对拍失败")

    def test_all(self):
        for case in self.datasets:
            for join_name in JOIN_SQL:
                with self.subTest(case=case, join=join_name):
                    self._check(case, join_name)
        self.report.sort()
        self.report.insert(0, "对拍总例数: %d" % (len(self.datasets) * len(JOIN_SQL)))
        self.report.insert(0, "null_join vs sqlite3 逐行对拍报告")


if __name__ == "__main__":
    unittest.main(verbosity=2)
