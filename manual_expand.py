"""对拍参考实现：逐字符、逐层手工推算，刻意不用任何缓存/备忘录。

与 style_expand.expand_document 独立编写，用于交叉验证（对拍）。
优先级：内联 > 段落 > 样式链(子覆盖父) > 默认值。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from style_expand import DEFAULT_PROPS, CharAttrs


def _walk_chain(styles: Dict[str, dict], name: Optional[str],
                diagnostics: List[str]) -> Dict[str, object]:
    """从根到叶手工走一遍继承链，每层都重新合并。"""
    if name is None:
        return {}
    if name not in styles:
        diagnostics.append(f"[断裂] 段落引用的样式 '{name}' 不存在；回退默认值")
        return {}

    # 先收集链（叶 -> 根），同时手工判环
    chain: List[str] = []
    seen = set()
    cur: Optional[str] = name
    while cur is not None:
        if cur not in styles:
            diagnostics.append(
                f"[断裂] 样式 '{chain[-1]}' 引用的样式 '{cur}' 不存在；回退默认值"
            )
            break
        if cur in seen:
            cycle = chain[chain.index(cur):] + [cur]
            diagnostics.append(
                "[循环] 样式循环引用: " + " -> ".join(cycle)
                + "；环上样式: " + ", ".join(sorted(set(cycle)))
            )
            # 在重复节点处截断：忽略回指链接，环上样式自身属性照常合并
            break
        seen.add(cur)
        chain.append(cur)
        cur = styles[cur].get("based_on")

    merged: Dict[str, object] = {}
    for style_name in reversed(chain):  # 根先铺，子后盖
        merged.update(styles[style_name].get("props", {}))
    return merged


def manual_expand(styles: Dict[str, dict], doc: List[dict],
                  defaults: Dict[str, object] = None) -> Tuple[List[List[CharAttrs]], List[str]]:
    defaults = dict(DEFAULT_PROPS if defaults is None else defaults)
    diagnostics: List[str] = []

    # 属性全集：默认值 + 样式 + 段落 + 内联里出现过的所有属性
    universe = set(defaults)
    for style in styles.values():
        universe.update(style.get("props", {}))
    for para in doc:
        universe.update(para.get("props", {}))
        for _, _, sp in para.get("spans", []):
            universe.update(sp)
    universe = sorted(universe)

    result: List[List[CharAttrs]] = []
    for para in doc:
        text = para["text"]
        para_props = para.get("props", {})
        spans = para.get("spans", [])
        rows: List[CharAttrs] = []
        for ci, _ch in enumerate(text):
            # 每个字符都重新走一遍样式链（刻意低效，保证独立）
            style_props = _walk_chain(styles, para.get("style"), diagnostics)
            attrs: CharAttrs = {}
            for prop in universe:
                value = None
                source = None
                # 内联层：逐个 span 扫描
                for s, e, sp in spans:
                    if s <= ci < e and prop in sp:
                        value, source = sp[prop], "inline"
                if source is None and prop in para_props:
                    value, source = para_props[prop], "paragraph"
                if source is None and prop in style_props:
                    value, source = style_props[prop], f"style:{para.get('style')}"
                if source is None:
                    if prop not in defaults:
                        raise ValueError(f"属性 '{prop}' 无默认值，拒绝静默丢弃")
                    value, source = defaults[prop], "default"
                attrs[prop] = (value, source)
            rows.append(attrs)
        result.append(rows)
    return result, diagnostics
