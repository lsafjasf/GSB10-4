"""生成固定对拍数据（确定性，种子写死），测试与交付都使用这份数据。"""
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)


def main():
    rng = random.Random(20261005)
    pool = ["key%04d" % i for i in range(300)]
    keys = []
    for _ in range(1000):
        keys.append(rng.choice(pool))
    # 边界键：空串、长键、非 ASCII
    keys += ["", "", "a-very-long-key-" + "z" * 200, "你好，世界", "你好，世界"]
    rng.shuffle(keys)

    with open(os.path.join(DATA, "sample_keys.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(keys) + "\n")
    expected = sorted(set(keys))
    with open(os.path.join(DATA, "sample_expected.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(expected) + "\n")

    M = (1 << 61) - 1
    with open(os.path.join(DATA, "collision_demo.txt"), "w") as fh:
        fh.write("# 64 位 CPython 上的整数哈希碰撞对（hash() 取模 2^61-1）\n")
        fh.write("# hash(0)            = %d\n" % hash(0))
        fh.write("# hash(2**61 - 1)    = %d\n" % hash(M))
        fh.write("%d\n%d\n" % (0, M))
    print("keys=%d expected_unique=%d" % (len(keys), len(expected)))
    print("hash(0)=%d hash(2**61-1)=%d" % (hash(0), hash(M)))


if __name__ == "__main__":
    main()
