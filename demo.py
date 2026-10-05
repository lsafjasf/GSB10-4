"""边界情形演示：单边、不连通、一侧为空、含多重边/自环。运行: python3 demo.py"""

from bvcover import LEFT, RIGHT, format_report, min_vertex_cover


def run(title, n, side, edges):
    print("=" * 64)
    print(title)
    print("-" * 64)
    print(format_report(min_vertex_cover(n, side, edges)))
    print()


run("情形1：单边", 2, [LEFT, RIGHT], [(0, 1)])

run("情形2：不连通（K2 + 路径4）", 6,
    [LEFT, RIGHT, LEFT, RIGHT, LEFT, RIGHT],
    [(0, 1), (2, 3), (3, 4), (4, 5)])

run("情形3：一侧为空（3 个点全在右侧，无跨侧边）", 3,
    [RIGHT, RIGHT, RIGHT], [])

run("情形4：多重边 + 自环 + 同侧边 + 反向输入", 4,
    [LEFT, LEFT, RIGHT, RIGHT],
    [(0, 2), (2, 0), (0, 2), (3, 2), (3, 2), (0, 0), (0, 1), (2, 3)])
