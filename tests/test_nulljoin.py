r"""自测：生产实现 vs 参考实现逐行对拍。

覆盖情形：
    - 完全没有空外键
    - 部分为空
    - 整列为空
    - 空值与非空混合
    - 边界用例（空表、键不在首列、重复键、None==None 不匹配等）
    - 随机模糊数据（多轮不同种子）

运行：python3 tests/test_nulljoin.py
产物：data/<场景>.<join_type>.tsv  （对拍数据，\N 表示空值）
"""

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nulljoin import JoinType, join, reference_join

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# ---------------------------------------------------------------- 场景数据
# 每张表: (左表, 右表, 左键列, 右键列)

SCENARIOS = {
    # 1. 完全没有空外键
    "no_nulls": (
        [(1, "a"), (2, "b"), (3, "c"), (2, "b2")],
        [(2, "X"), (3, "Y"), (4, "Z"), (2, "X2")],
        0, 0,
    ),
    # 2. 部分为空（左表含空外键）
    "partial_nulls_left": (
        [(1, "a"), (None, "n1"), (2, "b"), (None, "n2")],
        [(1, "X"), (2, "Y"), (3, "Z")],
        0, 0,
    ),
    # 3. 部分为空（右表含空外键）
    "partial_nulls_right": (
        [(1, "a"), (2, "b"), (3, "c")],
        [(1, "X"), (None, "n1"), (3, "Z"), (None, "n2")],
        0, 0,
    ),
    # 4. 整列为空（左表外键全空）
    "all_nulls_left": (
        [(None, "n1"), (None, "n2"), (None, "n3")],
        [(1, "X"), (2, "Y")],
        0, 0,
    ),
    # 5. 整列为空（右表外键全空）
    "all_nulls_right": (
        [(1, "a"), (2, "b")],
        [(None, "n1"), (None, "n2")],
        0, 0,
    ),
    # 6. 两侧整列都为空：None 与 None 也不得匹配
    "all_nulls_both": (
        [(None, "n1"), (None, "n2")],
        [(None, "m1"), (None, "m2")],
        0, 0,
    ),
    # 7. 空值与非空混合（两侧都混合，含重复键与字符串键）
    "mixed": (
        [("k1", 10), (None, 20), ("k2", 30), ("k1", 40), (None, 50), ("k3", 60)],
        [("k1", "X"), (None, "Y"), ("k1", "Z"), ("k9", "W"), (None, "V")],
        0, 0,
    ),
    # 8. 空表边界
    "empty_left": ([], [(1, "X"), (None, "n1")], 0, 0),
    "empty_right": ([(1, "a"), (None, "n1")], [], 0, 0),
    "empty_both": ([], [], 0, 0),
    # 9. 键不在首列 + 多列行
    "non_first_key_col": (
        [("r1", 7, "a"), ("r2", None, "b"), ("r3", 7, "c")],
        [("X", 7), ("Y", None), ("Z", 8)],
        1, 1,
    ),
    # 10. 单边重复键扇出
    "duplicate_keys": (
        [(5, "a"), (5, "b"), (5, "c")],
        [(5, "X"), (5, "Y")],
        0, 0,
    ),
}

JOIN_TYPES = list(JoinType)


# ---------------------------------------------------------------- 对拍工具
def fmt_row(row):
    return "\t".join(r"\N" if v is None else str(v) for v in row)


def dump_tsv(path, rows, ncols):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# " + "\t".join("c%d" % i for i in range(ncols)) + "\n")
        for row in rows:
            f.write(fmt_row(row) + "\n")


def check_scenario(name, left, right, lon, ron, dump):
    lw = max((len(r) for r in left), default=0)
    rw = max((len(r) for r in right), default=0)
    failures = []
    for jt in JOIN_TYPES:
        got = join(left, right, lon, ron, jt)
        want = reference_join(left, right, lon, ron, jt)
        if got != want:
            failures.append((name, jt.value, want, got))
        elif dump is not None:
            ncols = lw if jt in (JoinType.LEFT_SEMI, JoinType.LEFT_ANTI) else lw + rw
            dump_tsv(os.path.join(dump, "%s.%s.tsv" % (name, jt.value)), got, ncols)
    return failures


def fuzz(rounds, seed0, dump_failures=True):
    rng = random.Random(seed0)
    failures = []
    key_pool = [None, None, 1, 2, 3, 4, 5, "a", "b"]  # None 权重抬高
    for t in range(rounds):
        left = [tuple(rng.choice(key_pool) if c == 0 else rng.randrange(100)
                        for c in range(rng.randint(1, 3)))
                for _ in range(rng.randrange(0, 9))]
        right = [tuple(rng.choice(key_pool) if c == 0 else rng.randrange(100)
                       for c in range(rng.randint(1, 3)))
                 for _ in range(rng.randrange(0, 9))]
        lon = 0
        ron = 0
        for jt in JOIN_TYPES:
            got = join(left, right, lon, ron, jt)
            want = reference_join(left, right, lon, ron, jt)
            if got != want:
                failures.append(("fuzz#%d" % t, jt.value, want, got))
                if dump_failures and len(failures) <= 3:
                    print("FUZZ MISMATCH seed=%d round=%d %s" % (seed0, t, jt.value))
                    print("  left =", left)
                    print("  right=", right)
    return failures


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    all_failures = []

    for name, (left, right, lon, ron) in SCENARIOS.items():
        all_failures += check_scenario(name, left, right, lon, ron, dump=DATA_DIR)
        print("[scenario] %-22s ok" % name)

    for seed in (1, 2, 3):
        all_failures += fuzz(rounds=200, seed0=seed)
        print("[fuzz] seed=%d rounds=200 ok" % seed)

    # 专项断言：None 与 None 不匹配（内连接必须为空结果）
    assert join([(None,)], [(None,)], 0, 0, JoinType.INNER) == []
    assert reference_join([(None,)], [(None,)], 0, 0, JoinType.INNER) == []
    print("[assert] None==None never matches ok")

    if all_failures:
        print("\nFAILED: %d mismatch(es)" % len(all_failures))
        for name, jt, want, got in all_failures[:5]:
            print("  %s %s\n    want=%r\n    got =%r" % (name, jt, want, got))
        sys.exit(1)

    n_files = len([f for f in os.listdir(DATA_DIR) if f.endswith(".tsv")])
    print("\nALL PASS. %d 个场景 x %d 种关联逐行对拍一致；对拍数据 %d 个 TSV 已写入 data/"
          % (len(SCENARIOS), len(JOIN_TYPES), n_files))


if __name__ == "__main__":
    main()
