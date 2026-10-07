#!/usr/bin/env python3
"""自测：构建夹具主题树，逐条与 expected.json（手工核对数据）对拍。

覆盖情形：全部命中、全部缺失、只有低分辨率、扩展名大小写混用、
坏文件（截断 PNG / 坏签名 / 坏 SVG）、坏文件被父主题有效文件兜底、
目录大小写不敏感误命中防护、最近尺寸并列规则、扩展名冲突确定选取。
"""
import json
import os
import sys
import tempfile

from icon_theme import IconTheme, TraversalCounter

PNG_OK = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
          + b"\x00" * 13 + b"\x00" * 4)
SVG_OK = '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"/>\n'

FIXTURE = {
    "main/index.theme": "[Icon Theme]\nInherits=base\n",
    "base/index.theme": "[Icon Theme]\nInherits=\n",
    "hicolor/index.theme": "[Icon Theme]\n",
    # 全部命中
    "main/48/apps/ok-exact.png": PNG_OK,
    "main/scalable/apps/ok-scalable.svg": SVG_OK,
    # 只有低分辨率（请求 48，只有 16）
    "main/16/apps/only-low.png": PNG_OK,
    # 最近尺寸并列：32 与 64 距 48 同为 16，规则取较大者
    "main/32/apps/tie.png": PNG_OK,
    "main/64/apps/tie.png": PNG_OK,
    # 扩展名大小写混用
    "main/48/apps/case-ext.PNG": PNG_OK,
    "main/48/apps/mixed-case.Png": PNG_OK,
    "main/scalable/apps/case-svg.SVG": SVG_OK,
    # 坏文件：截断 PNG，被 base 主题同名有效文件兜底
    "main/48/apps/bad-png.png": PNG_OK[:20],
    "base/48/apps/bad-png.png": PNG_OK,
    # 坏文件：签名错误且无任何兜底
    "main/48/apps/bad-only.png": b"not a png, just junk padding......",
    # 坏文件：XML 未闭合的 SVG
    "main/scalable/apps/broken.svg": "<svg><unclosed",
    # 目录大小写：Apps 不等于 apps，应判违规且不得命中
    "main/48/Apps/wrong-case-dir.png": PNG_OK,
    # 同名不同大小写扩展名冲突：确定性选取全小写 .png
    "main/48/apps/dup.png": PNG_OK,
    "main/48/apps/dup.PNG": PNG_OK,
    # 约定违规样本
    "main/48/stray.png": PNG_OK,
    "main/20/apps/nonstandard.png": PNG_OK,
    "main/48/unknownctx/x.png": PNG_OK,
    "main/notes.txt": "not a theme file\n",
    # 父主题与 hicolor 兜底
    "base/48/apps/parent-hit.png": PNG_OK,
    "hicolor/48/apps/hicolor-hit.png": PNG_OK,
}

# (用例编号, 图标名, 上下文, 请求尺寸)
CASES = [
    ("q01", "ok-exact", "apps", 48),
    ("q02", "ok-scalable", "apps", 48),
    ("q03", "only-low", "apps", 48),
    ("q04", "tie", "apps", 48),
    ("q05", "case-ext", "apps", 48),
    ("q06", "mixed-case", "apps", 48),
    ("q07", "case-svg", "apps", 48),
    ("q08", "parent-hit", "apps", 48),
    ("q09", "hicolor-hit", "apps", 48),
    ("q10", "bad-png", "apps", 48),
    ("q11", "bad-only", "apps", 48),
    ("q12", "broken", "apps", 48),
    ("q13", "missing-icon", "apps", 48),
    ("q14", "wrong-case-dir", "apps", 48),
    ("q15", "dup", "apps", 48),
    ("q16", "nonstandard", "apps", 48),
]


def build_fixture(root):
    for rel, data in FIXTURE.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if isinstance(data, bytes):
            with open(path, "wb") as fh:
                fh.write(data)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(data)


def rel(root, path):
    return os.path.relpath(path, root) if path else None


def main():
    root = tempfile.mkdtemp(prefix="icontheme-fixture-")
    build_fixture(root)
    counter = TraversalCounter()
    theme = IconTheme(root, counter)

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "expected.json"), encoding="utf-8") as fh:
        expected = json.load(fh)

    failures = 0
    print(f"{'用例':<6}{'图标':<16}{'结果':<6}{'级别 / 状态'}")
    for qid, name, context, size in CASES:
        r = theme.resolve("main", name, context, size)
        actual = {
            "status": r.status,
            "level": r.level,
            "hit": rel(root, r.hit.path) if r.hit else None,
            "bad": [rel(root, b.path) for b in r.bad_files],
            "trace": [f"{t.level}:{t.outcome}" for t in r.trace],
        }
        exp = expected["cases"][qid]
        ok = actual == exp
        failures += not ok
        print(f"{qid:<6}{name:<16}{'PASS' if ok else 'FAIL':<6}"
              f"{r.level or r.status}")
        if not ok:
            print("  expected:", json.dumps(exp, ensure_ascii=False))
            print("  actual  :", json.dumps(actual, ensure_ascii=False))

    violations, bad_files = theme.validate()
    actual_v = sorted((v.kind, rel(root, v.path)) for v in violations)
    expected_v = sorted(tuple(x) for x in expected["violations"])
    v_ok = actual_v == expected_v
    failures += not v_ok
    print(f"\n目录约定违规 {len(actual_v)} 条: {'PASS' if v_ok else 'FAIL'}")
    if not v_ok:
        print("  expected:", expected_v)
        print("  actual  :", actual_v)

    actual_b = sorted(rel(root, b.path) for b in bad_files)
    expected_b = sorted(expected["bad_files"])
    b_ok = actual_b == expected_b
    failures += not b_ok
    print(f"坏文件 {len(actual_b)} 个: {'PASS' if b_ok else 'FAIL'}")
    if not b_ok:
        print("  expected:", expected_b)
        print("  actual  :", actual_b)

    print(f"\n目录遍历 scandir={counter.scandir} 次, "
          f"文件探测 file_probe={counter.file_probe} 次")
    print("自测", "全部通过" if failures == 0 else f"有 {failures} 处不一致")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
