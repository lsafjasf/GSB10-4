"""书签库自测（仅标准库）。

运行：python3 tests/selftest.py
退出码 0 = 全部通过，1 = 有用例失败。
产物：
- roundtrip/*.out.json、roundtrip/SHA256SUMS.txt  无损往返对拍数据
- results/anomaly_report.{json,txt}               异常报告汇总
"""

import hashlib
import json as stdlib_json
import os
import random
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bookmarks import iterjson  # noqa: E402
from bookmarks.model import (  # noqa: E402
    BookmarkDocument,
    BookmarkFormatError,
    FORMAT_ID,
    FORMAT_VERSION,
)
from bookmarks.iterjson import JSONParseError  # noqa: E402

FIX = os.path.join(ROOT, "fixtures")
ANOM = os.path.join(FIX, "anomalies")
RT = os.path.join(ROOT, "roundtrip")
RES = os.path.join(ROOT, "results")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 基础 JSON

def test_json_basics():
    print("[1] 迭代 JSON：基本类型、转义、Unicode、数字")
    cases = [
        0, -1, 42, 3.14, 1e20, -0.5, True, False, None,
        "", "中文 🚀", '引号"反斜杠\\', "换行\n制表\t退格\b",
        [], {}, [1, [2, [3]], {"a": "b"}],
        {"z": 1, "a": [True, None, -7], "m": {"x": 0.0}},
    ]
    for value in cases:
        text = iterjson.dumps(value, indent=None)
        # 与标准库对拍：语义一致
        check(f"json 对拍 {text[:24]!r}", iterjson.loads(text) == value
              and stdlib_json.loads(text) == value)

    # 键顺序保留
    text = iterjson.dumps({"z": 1, "a": 2, "m": 3}, indent=None)
    check("键顺序保留", text == '{"z":1,"a":2,"m":3}\n')

    # 错误定位
    for bad, needle in [
        ("{,}", "逗号"),
        ('{"a" 1}', "冒号"),
        ("[1,]", "逗号"),
        ('{"a":}', "缺少对应的值"),
        ("tru", "非法字面量"),
        ("1.2.3", "非法数字"),
        ("[1 2]", "缺少逗号"),
    ]:
        try:
            iterjson.loads(bad)
            check(f"应报错 {bad!r}", False)
        except JSONParseError as exc:
            check(f"报错定位 {bad!r}", needle in str(exc) and exc.line >= 1)


def test_json_random_vs_stdlib():
    print("[2] 迭代 JSON：与标准库 json 随机对拍 200 例")
    rng = random.Random(42)

    def gen(depth):
        kind = rng.randrange(6)
        if kind == 0 or depth == 0:
            return rng.choice([rng.randint(-100000, 100000),
                               round(rng.uniform(-1e6, 1e6), 6),
                               "中文", "a\"b\\c\n", True, False, None])
        if kind in (1, 2):
            return [gen(depth - 1) for _ in range(rng.randrange(5))]
        return {f"k{rng.randrange(100)}": gen(depth - 1)
                for _ in range(rng.randrange(5))}

    for _ in range(200):
        value = gen(4)
        ours_text = iterjson.dumps(value, indent=None)
        std_text = stdlib_json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if iterjson.loads(ours_text) != iterjson.loads(std_text) \
                or stdlib_json.loads(ours_text) != value:
            check("随机对拍", False, repr(value)[:120])
            return
    check("随机对拍", True)


def test_json_deep_no_recursion():
    print("[3] 迭代 JSON：10 万层嵌套，递归上限压到 300 仍不溢出")
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(300)
    try:
        depth = 100_000
        arr_text = "[" * depth + "1" + "]" * depth + "\n"
        value = iterjson.loads(arr_text[:-1])
        check("深层数组解析", len(value) == 1)
        check("深层数组写回", iterjson.dumps(value, indent=None) == arr_text)

        node = 1
        for _ in range(depth):
            node = {"k": node}
        text = iterjson.dumps(node, indent=None)
        check("深层对象解析/写回",
              iterjson.dumps(iterjson.loads(text), indent=None) == text)
    finally:
        sys.setrecursionlimit(old_limit)


# --------------------------------------------------------- 合法样本往返

VALID = [
    "single_node.json",
    "empty_folder.json",
    "empty_roots.json",
    "nested_mixed.json",
    "order_check.json",
    "deep_5000.json",
]


def test_fixture_roundtrip():
    print("[4] 合法样本：读入 -> 写出，字节完全一致（对拍数据落盘）")
    os.makedirs(RT, exist_ok=True)
    sums = []
    for name in VALID:
        src = read(os.path.join(FIX, name))
        compact = name == "deep_5000.json"
        doc = BookmarkDocument.from_text(src)
        out = doc.to_text(indent=None if compact else 2)
        check(f"无异常 {name}", doc.report.ok, doc.report.to_text())
        check(f"字节一致 {name}", out == src)
        out_path = os.path.join(RT, name.replace(".json", ".out.json"))
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(out)
        h_in, h_out = sha(src), sha(out)
        sums.append(f"{h_in}  fixtures/{name}")
        sums.append(f"{h_out}  roundtrip/{os.path.basename(out_path)}  [MATCH]")
    with open(os.path.join(RT, "SHA256SUMS.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(sums) + "\n")
    print(f"     对拍数据已写入 roundtrip/ 与 roundtrip/SHA256SUMS.txt")


def test_order_preserved():
    print("[5] 兄弟顺序：按结构数组下标，不按标题排序")
    doc = BookmarkDocument.from_text(read(os.path.join(FIX, "order_check.json")))
    root_names = [n["name"] for n in doc.data["roots"]]
    child_names = [n["name"] for n in doc.data["roots"][3]["children"]]
    check("根层顺序", root_names == ["zebra", "apple", "mango", "目录"], str(root_names))
    check("子层顺序", child_names == ["delta", "beta", "alpha"], str(child_names))
    check("非排序可证", child_names != sorted(child_names))


# ------------------------------------------------------------- 异常处理

def test_anomalies():
    print("[6] 异常样本：异常码、修复结果与期望逐字节一致")
    os.makedirs(RES, exist_ok=True)
    summary = {}
    all_ok = True
    for fname in sorted(os.listdir(ANOM)):
        if not fname.endswith(".json") or ".expected." in fname or ".report." in fname:
            continue
        case = fname[:-5]
        src = read(os.path.join(ANOM, fname))
        doc = BookmarkDocument.from_text(src)
        codes = [i.code for i in doc.report.issues]
        expected_codes = iterjson.loads(
            read(os.path.join(ANOM, f"{case}.report.json"))
        )["expected_codes"]
        expected_text = read(os.path.join(ANOM, f"{case}.expected.json"))
        out = doc.to_text()
        ok_codes = codes == expected_codes
        ok_bytes = out == expected_text
        check(f"异常码 {case}", ok_codes, f"{codes} != {expected_codes}")
        check(f"修复结果 {case}", ok_bytes)
        if not (ok_codes and ok_bytes):
            all_ok = False
        summary[case] = doc.report.to_dict()
    check("全部异常样本通过", all_ok)

    # 人类可读 + 机器可读报告落盘
    lines = ["书签异常报告（由 tests/selftest.py 汇总 fixtures/anomalies 生成）", "=" * 60]
    for case, rep in summary.items():
        lines.append(f"\n### {case}.json：{rep['issue_count']} 处异常")
        for issue in rep["issues"]:
            lines.append(
                f"  [{issue['code']}] 处理={issue['action']} "
                f"位置={issue['path'] or '-'} 节点={issue['node_id'] or '-'}\n"
                f"    {issue['message']}"
            )
    with open(os.path.join(RES, "anomaly_report.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(RES, "anomaly_report.json"), "w", encoding="utf-8") as fh:
        fh.write(iterjson.dumps(summary, indent=2))
    print("     异常报告已写入 results/anomaly_report.{txt,json}")


# ------------------------------------------------------------- 边界用例

def test_edge_cases():
    print("[7] 边界：单节点 / 空目录 / 空树 / 超宽 / 超深文档 / 致命错误")

    single = read(os.path.join(FIX, "single_node.json"))
    doc = BookmarkDocument.from_text(single)
    check("单节点", doc.stats()["nodes"] == 1 and doc.to_text() == single)

    empty_folder = read(os.path.join(FIX, "empty_folder.json"))
    doc = BookmarkDocument.from_text(empty_folder)
    check("空目录", doc.stats() == {"nodes": 1, "folders": 1, "urls": 0,
                                    "other_types": 0, "max_depth": 1, "roots": 1})

    doc = BookmarkDocument.from_text(read(os.path.join(FIX, "empty_roots.json")))
    check("空树", doc.stats()["nodes"] == 0 and doc.report.ok)

    # 超宽：2 万个兄弟
    roots = [{"id": f"w{i}", "type": "url", "name": f"宽 {i}",
              "url": f"https://w{i}.example.com/"} for i in range(20_000)]
    text = iterjson.dumps(
        {"format": FORMAT_ID, "version": FORMAT_VERSION, "roots": roots})
    doc = BookmarkDocument.from_text(text)
    check("超宽 2 万兄弟", doc.stats()["nodes"] == 20_000
          and doc.to_text() == text)

    # 超深文档：5 万层单链，递归上限压到 300
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(300)
    try:
        depth = 50_000
        node = {"id": "leaf", "type": "url", "name": "底",
                "url": "https://deep/"}
        for level in range(depth, 0, -1):
            node = {"id": f"l{level}", "type": "folder", "name": f"L{level}",
                    "children": [node]}
        text = iterjson.dumps(
            {"format": FORMAT_ID, "version": FORMAT_VERSION, "roots": [node]},
            indent=None)
        doc = BookmarkDocument.from_text(text)
        check("超深 5 万层解析", doc.stats()["max_depth"] == depth + 1)
        check("超深 5 万层写回", doc.to_text(indent=None) == text)
    finally:
        sys.setrecursionlimit(old_limit)

    # 致命错误
    try:
        BookmarkDocument.from_text("{不是 json")
        check("非法 JSON 致命错误", False)
    except JSONParseError:
        check("非法 JSON 致命错误", True)
    try:
        BookmarkDocument.from_text('{"format": "other", "version": 1, "roots": []}')
        check("错误 format 致命错误", False)
    except BookmarkFormatError:
        check("错误 format 致命错误", True)


def test_cli_smoke():
    print("[8] CLI 冒烟：python3 -m bookmarks stats / check / roundtrip")
    env = dict(os.environ, PYTHONPATH=ROOT)
    p = subprocess.run(
        [sys.executable, "-m", "bookmarks", "stats",
         os.path.join(FIX, "nested_mixed.json")],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    check("stats 子命令", p.returncode == 0 and '"max_depth": 3' in p.stdout,
          p.stderr)

    out_path = os.path.join(RT, "cli_smoke.out.json")
    p = subprocess.run(
        [sys.executable, "-m", "bookmarks", "roundtrip",
         os.path.join(FIX, "nested_mixed.json"), out_path],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    check("roundtrip 子命令", p.returncode == 0 and "字节完全一致" in p.stdout,
          p.stdout + p.stderr)

    p = subprocess.run(
        [sys.executable, "-m", "bookmarks", "check",
         os.path.join(ANOM, "orphan_reference.json")],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    check("check 子命令（异常退出码 1）",
          p.returncode == 1 and "orphan_reference" in p.stdout, p.stdout + p.stderr)


def main():
    test_json_basics()
    test_json_random_vs_stdlib()
    test_json_deep_no_recursion()
    test_fixture_roundtrip()
    test_order_preserved()
    test_anomalies()
    test_edge_cases()
    test_cli_smoke()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
