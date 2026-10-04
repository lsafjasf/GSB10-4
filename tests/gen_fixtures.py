"""生成 fixtures/ 下的全部测试数据（合法样本 + 异常样本 + 期望结果）。

运行：python3 tests/gen_fixtures.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bookmarks import iterjson  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures")
ANOM = os.path.join(FIX, "anomalies")


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"  写入 {os.path.relpath(path, ROOT)}  ({len(text.encode('utf-8'))} 字节)")


def envelope(roots, **extra):
    doc = {"format": "gsb-bookmarks", "version": 1, "roots": roots}
    doc.update(extra)
    return doc


def url(nid, name, link, **extra):
    node = {"id": nid, "type": "url", "name": name, "url": link}
    node.update(extra)
    return node


def folder(nid, name, children, **extra):
    node = {"id": nid, "type": "folder", "name": name, "children": children}
    node.update(extra)
    return node


def gen_valid():
    # 1. 单节点
    write(os.path.join(FIX, "single_node.json"), iterjson.dumps(envelope([
        url("n1", "单个书签", "https://example.com/",
            add_date="1759600000", tags=["单节点", "测试"]),
    ])))

    # 2. 空目录
    write(os.path.join(FIX, "empty_folder.json"), iterjson.dumps(envelope([
        folder("f1", "空目录", []),
    ])))

    # 3. 空 roots（整棵树为空）
    write(os.path.join(FIX, "empty_roots.json"), iterjson.dumps(envelope([])))

    # 4. 嵌套混合：多层目录、Unicode、转义字符、未知扩展字段、parent_id 校验字段
    mixed = envelope([
        folder("f1", "工作", [
            url("n1", "GitHub", "https://github.com/",
                add_date="1759600001", icon=None, visit_count=0, starred=True),
            folder("f2", "文档", [
                url("n2", "API 参考", "https://docs.example.com/api",
                    parent_id="f2", x_meta={"权重": 3, "标签": ["a", "b"]}),
                url("n3", "转义字符 \"引号\" \\反斜杠\\ 换行\n制表\t",
                    "https://example.com/escape"),
            ]),
            folder("f3", "空子目录", []),
        ], add_date="1759600002"),
        folder("f4", "个人 🚀 中文目录", [
            url("n4", " emoji 书签 😀", "https://emoji.example.com/"),
        ]),
        url("n5", "根级链接", "https://root.example.com/", parent_id=""),
    ], meta={"导出工具": "bookmarks-cli", "导出时间": "2026-10-04T00:00:00+08:00"})
    write(os.path.join(FIX, "nested_mixed.json"), iterjson.dumps(mixed))

    # 5. 顺序样本：标题故意不按字典序，验证写回保持结构顺序而非标题排序
    write(os.path.join(FIX, "order_check.json"), iterjson.dumps(envelope([
        url("o1", "zebra", "https://z.example.com/"),
        url("o2", "apple", "https://a.example.com/"),
        url("o3", "mango", "https://m.example.com/"),
        folder("of", "目录", [
            url("o4", "delta", "https://d.example.com/"),
            url("o5", "beta", "https://b.example.com/"),
            url("o6", "alpha", "https://al.example.com/"),
        ]),
    ])))

    # 6. 超深嵌套：5000 层单链目录（紧凑格式存储，避免缩进体积爆炸）
    depth = 5000
    node = url("d-leaf", "最深处的书签", "https://deep.example.com/")
    for level in range(depth, 0, -1):
        node = folder(f"d{level}", f"第 {level} 层", [node])
    write(os.path.join(FIX, "deep_5000.json"),
          iterjson.dumps(envelope([node]), indent=None))


def gen_anomalies():
    cases = []

    # 1. 重复标识：两个不同节点共用 id
    inp = envelope([
        folder("fa", "目录A", [url("dup-1", "第一个", "https://a.example.com/")]),
        folder("fb", "目录B", [url("dup-1", "第二个", "https://b.example.com/")]),
    ])
    exp = inp  # 处理结果：两处均保留，文件不变
    cases.append(("duplicate_id", inp, exp, ["duplicate_id"]))

    # 2. 父子关系不一致：parent_id 指向另一个已存在的目录
    inp = envelope([
        folder("f1", "目录一", [
            url("c1", "子节点", "https://c.example.com/", parent_id="f2"),
        ]),
        folder("f2", "目录二", []),
    ])
    exp = envelope([
        folder("f1", "目录一", [
            url("c1", "子节点", "https://c.example.com/", parent_id="f1"),
        ]),
        folder("f2", "目录二", []),
    ])
    cases.append(("parent_mismatch", inp, exp, ["parent_mismatch"]))

    # 3. 孤立节点：parent_id 指向不存在的节点
    inp = envelope([
        folder("f1", "目录", [
            url("c1", "孤儿", "https://orphan.example.com/", parent_id="ghost-999"),
        ]),
    ])
    exp = envelope([
        folder("f1", "目录", [
            url("c1", "孤儿", "https://orphan.example.com/", parent_id="f1"),
        ]),
    ])
    cases.append(("orphan_reference", inp, exp, ["orphan_reference"]))

    # 4. 链接节点下挂子节点：迁移到根列表末尾，保持相对顺序
    inp = envelope([
        url("u1", "错误地带了孩子", "https://u1.example.com/"),
    ])
    inp["roots"][0]["children"] = [
        url("k1", "孩子一", "https://k1.example.com/"),
        folder("k2", "孩子二", [url("k3", "孙子", "https://k3.example.com/")]),
    ]
    exp = envelope([
        url("u1", "错误地带了孩子", "https://u1.example.com/", children=[]),
        url("k1", "孩子一", "https://k1.example.com/"),
        folder("k2", "孩子二", [url("k3", "孙子", "https://k3.example.com/")]),
    ])
    cases.append(("children_under_url", inp, exp, ["children_under_url"]))

    # 5. id 缺失：自动分配（id 追加在对象末尾）
    inp = envelope([
        {"type": "url", "name": "无 id 节点", "url": "https://noid.example.com/"},
    ])
    exp = envelope([
        {"type": "url", "name": "无 id 节点", "url": "https://noid.example.com/",
         "id": "auto-1"},
    ])
    cases.append(("missing_id", inp, exp, ["id_missing"]))

    # 6. children 类型非法：按空目录处理
    inp = envelope([
        {"id": "f1", "type": "folder", "name": "坏目录", "children": "oops"},
    ])
    exp = envelope([
        {"id": "f1", "type": "folder", "name": "坏目录", "children": []},
    ])
    cases.append(("invalid_children", inp, exp, ["invalid_children"]))

    for name, inp, exp, codes in cases:
        write(os.path.join(ANOM, f"{name}.json"), iterjson.dumps(inp))
        write(os.path.join(ANOM, f"{name}.expected.json"), iterjson.dumps(exp))
        write(os.path.join(ANOM, f"{name}.report.json"),
              iterjson.dumps({"expected_codes": codes}))


def main():
    print("生成合法样本：")
    gen_valid()
    print("生成异常样本：")
    gen_anomalies()
    print("完成。")


if __name__ == "__main__":
    main()
