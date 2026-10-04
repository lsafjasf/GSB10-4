# -*- coding: utf-8 -*-
"""bookmarks 库自测：无损往返对拍、异常报告、边界用例。

运行：python3 test_bookmarks.py
产物：out/roundtrip_report.json（对拍数据）、out/anomaly_report.json（异常报告）、
      out/edge_cases.json（边界用例结果）、out/roundtrip/*.html（写回样本）。
"""

import json
import os
import sys

sys.setrecursionlimit(100)  # 压低递归上限：若库内有递归实现会立刻暴露

import bookmarks

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
OUT = os.path.join(HERE, "out")
ROUNDTRIP_DIR = os.path.join(OUT, "roundtrip")

DEEP_DEPTH = 10000  # 超深嵌套用例深度


def build_deep_tree(depth):
    """depth 层目录嵌套，最底层放 1 个书签。"""
    tree = bookmarks.BookmarkTree()
    node = tree.root
    for level in range(depth):
        folder = bookmarks.Node("folder", title="d%d" % level)
        folder.attrs["ID"] = "f%d" % level
        node.children.append(folder)
        folder.parent = node
        node = folder
    leaf = bookmarks.Node("bookmark", title="leaf")
    leaf.attrs["HREF"] = "https://leaf.example/"
    node.children.append(leaf)
    leaf.parent = node
    return tree


def build_single_tree():
    tree = bookmarks.BookmarkTree()
    only = bookmarks.Node("bookmark", title="唯一书签")
    only.attrs["HREF"] = "https://only.example/"
    only.attrs["ADD_DATE"] = "1700000000"
    tree.root.children.append(only)
    only.parent = tree.root
    return tree


def build_orphan_tree():
    """PID 指向不存在 ID 的孤立节点 + 正常节点。"""
    tree = bookmarks.BookmarkTree()
    folder = bookmarks.Node("folder", title="容器")
    folder.attrs["ID"] = "f1"
    tree.root.children.append(folder)
    folder.parent = tree.root
    ok = bookmarks.Node("bookmark", title="正常")
    ok.attrs.update([("ID", "b1"), ("PID", "f1"), ("HREF", "https://ok.example/")])
    folder.children.append(ok)
    ok.parent = folder
    orphan = bookmarks.Node("bookmark", title="孤立节点")
    orphan.attrs.update([("ID", "b2"), ("PID", "ghost"), ("HREF", "https://o.example/")])
    folder.children.append(orphan)
    orphan.parent = folder
    return tree


def check_roundtrip(name, source_text, roundtrip_records, indent=4):
    """解析 -> 写回 -> 再解析，做结构与字节双重对拍，返回记录。"""
    tree1 = bookmarks.parse_string(source_text)
    out1 = bookmarks.serialize(tree1, indent=indent)
    tree2 = bookmarks.parse_string(out1)
    out2 = bookmarks.serialize(tree2, indent=indent)

    total, depth = bookmarks.stats(tree1)
    digest1 = bookmarks.tree_digest(tree1)
    digest2 = bookmarks.tree_digest(tree2)
    record = {
        "case": name,
        "nodes": total,
        "max_depth": depth,
        "input_bytes": len(source_text.encode("utf-8")),
        "output_bytes": len(out1.encode("utf-8")),
        "sha256_pass1": digest1,
        "sha256_pass2": digest2,
        "structural_equal": bookmarks.trees_equal(tree1, tree2),
        "byte_identical_second_pass": out1 == out2,
        "byte_identical_to_input": out1 == source_text,
        "anomalies": len(tree1.anomalies) + len(bookmarks.validate(tree1)),
    }
    assert record["structural_equal"], "%s: 结构对拍失败" % name
    assert record["byte_identical_second_pass"], "%s: 二次写回不稳定" % name
    assert digest1 == digest2, "%s: 摘要不一致" % name
    roundtrip_records.append(record)

    safe = name.replace("/", "_")
    with open(os.path.join(ROUNDTRIP_DIR, safe + ".html"), "w", encoding="utf-8") as fh:
        fh.write(out1)
    return tree1


def main():
    os.makedirs(ROUNDTRIP_DIR, exist_ok=True)
    roundtrip_records = []
    anomaly_report = {}
    edge_cases = {}

    # ---- 1. 规范化嵌套样本：层级/顺序/属性/描述/文档头，字节级往返 ----
    with open(os.path.join(FIXTURES, "canonical_nested.html"), encoding="utf-8") as fh:
        canonical_text = fh.read()
    tree = check_roundtrip("canonical_nested", canonical_text, roundtrip_records)
    assert tree.anomalies == [] and bookmarks.validate(tree) == []
    # 顺序依据验证：结构顺序保持文件出现顺序，不按标题排序
    work = tree.root.children[0]
    titles = [n.title for n in work.children]
    assert titles == ["链接 & 一", "Zebra 排在 Apple 前面", "Apple", "空目录"], titles
    # 附属属性（含大小写与书写顺序）原样保留
    first = work.children[0]
    assert list(first.attrs.items()) == [
        ("HREF", "https://a.example/1?x=1&y=2"),
        ("ADD_DATE", "1700000000"),
        ("custom-Attr", 'v"x'),
    ]
    assert work.description == "工作相关内容 & 笔记"
    assert tree.h1 == "我的书签"
    edge_cases["empty_folder"] = {
        "描述": "空目录解析后保留且写回为空目录",
        "结果": work.children[3].is_folder() and work.children[3].children == [],
    }
    assert edge_cases["empty_folder"]["结果"]

    # ---- 2. 单节点 ----
    single_text = bookmarks.serialize(build_single_tree())
    tree = check_roundtrip("single_node", single_text, roundtrip_records)
    assert len(tree.root.children) == 1 and tree.root.children[0].kind == "bookmark"
    edge_cases["single_node"] = {"描述": "整份文件只有 1 个书签节点", "结果": True}

    # ---- 3. 超深嵌套（10000 层，紧凑输出）----
    deep_text = bookmarks.serialize(build_deep_tree(DEEP_DEPTH), indent=0)
    tree = check_roundtrip("deep_nesting_10000", deep_text, roundtrip_records, indent=0)
    total, depth = bookmarks.stats(tree)
    assert depth == DEEP_DEPTH + 1 and total == DEEP_DEPTH + 1
    edge_cases["deep_nesting"] = {
        "描述": "%d 层目录嵌套，迭代解析/写回，无递归溢出" % DEEP_DEPTH,
        "深度": depth, "结果": True,
    }

    # ---- 4. 孤立节点（PID 悬空）----
    orphan_text = bookmarks.serialize(build_orphan_tree())
    tree = check_roundtrip("orphan_node", orphan_text, roundtrip_records)
    anomalies = bookmarks.validate(tree)
    kinds = [a["kind"] for a in anomalies]
    assert kinds == ["dangling_parent_ref"], kinds
    assert anomalies[0]["pid"] == "ghost"
    anomaly_report["orphan_node"] = anomalies
    edge_cases["orphan_node"] = {
        "描述": "PID 指向不存在的 ID，报告为孤立节点并保持结构位置",
        "结果": kinds,
    }

    # ---- 5. 结构异常样本：stray / 未闭合 DL ----
    with open(os.path.join(FIXTURES, "malformed.html"), encoding="utf-8") as fh:
        malformed_text = fh.read()
    tree = bookmarks.parse_string(malformed_text)
    kinds = [a["kind"] for a in tree.anomalies]
    assert kinds.count("stray_node") == 1, kinds
    assert kinds.count("unclosed_list") == 2, kinds
    # stray 条目被挂到根目录，未闭合目录自动闭合，内容不丢失
    titles = [n.title for n in tree.root.children]
    assert titles == ["根 DL 之外的孤立条目", "正常目录"], titles
    normal = tree.root.children[1]
    assert normal.children[1].title == "未闭合目录"
    assert normal.children[1].children[0].title == "x"
    anomaly_report["malformed_structure"] = tree.anomalies
    # 异常修复后写回，再解析应无异常且结构稳定
    out1 = bookmarks.serialize(tree)
    tree2 = bookmarks.parse_string(out1)
    assert tree2.anomalies == [] and bookmarks.trees_equal(tree, tree2)
    roundtrip_records.append({
        "case": "malformed_repaired",
        "nodes": bookmarks.stats(tree)[0],
        "max_depth": bookmarks.stats(tree)[1],
        "input_bytes": len(malformed_text.encode("utf-8")),
        "output_bytes": len(out1.encode("utf-8")),
        "sha256_pass1": bookmarks.tree_digest(tree),
        "sha256_pass2": bookmarks.tree_digest(tree2),
        "structural_equal": True,
        "byte_identical_second_pass": out1 == bookmarks.serialize(tree2),
        "byte_identical_to_input": False,
        "anomalies": len(tree.anomalies),
        "备注": "输入含结构异常，修复后写回，二次解析无异常且结构一致",
    })

    # ---- 6. ID/PID 校验：重复标识、父子不一致、孤立 ----
    with open(os.path.join(FIXTURES, "validate_ids.html"), encoding="utf-8") as fh:
        ids_text = fh.read()
    tree = check_roundtrip("validate_ids", ids_text, roundtrip_records)
    anomalies = bookmarks.validate(tree)
    by_kind = {}
    for item in anomalies:
        by_kind.setdefault(item["kind"], []).append(item)
    assert len(by_kind.get("duplicate_id", [])) == 1
    assert by_kind["duplicate_id"][0]["id"] == "b3"
    assert len(by_kind.get("dangling_parent_ref", [])) == 1
    assert by_kind["dangling_parent_ref"][0]["pid"] == "f999"
    assert len(by_kind.get("parent_mismatch", [])) == 1
    assert by_kind["parent_mismatch"][0]["pid"] == "f1"
    assert by_kind["parent_mismatch"][0]["structural_parent_id"] == "f2"
    anomaly_report["validate_ids"] = anomalies
    # 校验只报告不改写：往返依然无损
    assert bookmarks.trees_equal(tree, bookmarks.parse_string(bookmarks.serialize(tree)))

    # ---- 写报告 ----
    summary = {
        "顺序依据": "节点按文件中的结构顺序（<DT> 条目在父 <DL> 内的出现顺序）还原，"
                    "不做任何按标题的排序；canonical_nested 用例中 Zebra 排在 Apple 前即为证明。",
        "往返判定": "structural_equal（层级+兄弟顺序+标题+属性及属性顺序+描述+文档头逐节点比对）"
                    "且 byte_identical_second_pass（二次写回字节一致）；"
                    "byte_identical_to_input 表示与原始输入字节一致（规范化输入可满足）。",
        "用例": roundtrip_records,
    }
    with open(os.path.join(OUT, "roundtrip_report.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT, "anomaly_report.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "处理约定": {
                "duplicate_id": "保留先出现者作为该 ID 的规范节点，重复节点原样保留（不删不改）",
                "parent_mismatch": "以结构嵌套位置为准，PID 仅作参考元数据，原样保留",
                "dangling_parent_ref": "判定为孤立节点，保持其结构位置，原样保留",
                "stray_node": "挂到根目录末尾（按出现顺序），写回后成为合法结构",
                "unclosed_list": "文件末尾自动闭合，写回后成为合法结构",
                "anonymous_list": "按匿名目录处理，其内容上提一级",
            },
            "报告": anomaly_report,
        }, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT, "edge_cases.json"), "w", encoding="utf-8") as fh:
        json.dump(edge_cases, fh, ensure_ascii=False, indent=2)

    print("全部断言通过。对拍 %d 组，异常报告 %d 组，边界用例 %d 个。"
          % (len(roundtrip_records), len(anomaly_report), len(edge_cases)))
    for record in roundtrip_records:
        print("  %-22s 节点=%-6d 深度=%-6d 结构一致=%s 二次写回字节一致=%s 与输入字节一致=%s"
              % (record["case"], record["nodes"], record["max_depth"],
                 record["structural_equal"], record["byte_identical_second_pass"],
                 record["byte_identical_to_input"]))


if __name__ == "__main__":
    main()
