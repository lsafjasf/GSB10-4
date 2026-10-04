"""样式继承展开库（纯标准库）。

三层优先级（高 -> 低）：

    内联(span) > 段落直接属性(paragraph) > 段落样式链(style) > 文档默认(default)

段落样式可通过 ``based_on`` 继承另一个样式，子类同名属性覆盖父类。
继承链缺失（引用的样式不存在）会记录诊断并回退到显式默认值；
循环引用会被识别，诊断信息列出环上的全部样式名，并在环处断链、回退默认值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 明确写出的默认值——继承链断裂时回退到这里，绝不静默丢属性。
DEFAULT_PROPS: Dict[str, object] = {
    "font": "serif",
    "size": 12,
    "bold": False,
    "italic": False,
    "color": "black",
}


@dataclass
class StyleSheet:
    """命名样式表。

    styles[name] = {"props": {属性: 值}, "based_on": 父样式名或 None}
    """

    styles: Dict[str, Dict[str, object]]
    defaults: Dict[str, object] = field(default_factory=lambda: dict(DEFAULT_PROPS))
    diagnostics: List[str] = field(default_factory=list)
    _resolved: Dict[str, Dict[str, object]] = field(default_factory=dict, init=False)
    _reported: set = field(default_factory=set, init=False)

    def resolve_all(self) -> None:
        """展开全部样式。迭代实现，继承深度没有硬上限。"""
        for name in self.styles:
            if name not in self._resolved:
                self._resolve_from(name)

    def resolved(self, name: Optional[str]) -> Dict[str, object]:
        """返回某个样式沿继承链合并后的属性；未知/断裂链返回 {}（后续回退默认值）。"""
        if name is None:
            return {}
        if name not in self._resolved:
            if name not in self.styles:
                self._missing(name, None)
                return {}
            self._resolve_from(name)
        return self._resolved[name]

    def _missing(self, missing: str, referrer: Optional[str]) -> None:
        key = ("missing", missing, referrer)
        if key in self._reported:
            return
        self._reported.add(key)
        ref = f"样式 '{referrer}'" if referrer else "段落"
        self.diagnostics.append(
            f"[断裂] {ref} 引用的样式 '{missing}' 不存在；"
            f"该分支回退到默认值，未显式设置的属性不会丢失。"
        )

    def _resolve_from(self, start: str) -> None:
        """沿 based_on 收集链，逆序合并；识别缺失与环，全程迭代、无递归深度限制。"""
        chain: List[str] = []
        on_chain = set()
        cur: Optional[str] = start
        base: Dict[str, object] = {}

        while cur is not None:
            if cur in self._resolved:
                base = self._resolved[cur]
                break
            if cur not in self.styles:
                self._missing(cur, chain[-1] if chain else None)
                break
            if cur in on_chain:
                cut = chain.index(cur)
                cycle = chain[cut:] + [cur]
                ckey = ("cycle", frozenset(cycle))
                if ckey not in self._reported:
                    self._reported.add(ckey)
                    self.diagnostics.append(
                        "[循环] 样式循环引用: "
                        + " -> ".join(cycle)
                        + "；环上样式: "
                        + ", ".join(sorted(set(cycle)))
                        + "。在环处断链，环内未设置的属性回退默认值。"
                    )
                # 在重复节点处截断：忽略回指链接，以 {} 为基底，
                # 环上样式自身显式属性照常合并，未设置的属性回退默认值。
                break
            on_chain.add(cur)
            chain.append(cur)
            cur = self.styles[cur].get("based_on")

        merged = dict(base)
        for name in reversed(chain):
            merged = {**merged, **self.styles[name].get("props", {})}
            self._resolved[name] = merged


# 每个属性带“胜出来源”，便于核对优先级。
# 取值为 (value, source)；source ∈ {"inline","paragraph",f"style:{name}","default"}。
CharAttrs = Dict[str, Tuple[object, str]]


def _property_universe(sheet: StyleSheet, doc: List[dict]) -> List[str]:
    names = set(sheet.defaults)
    for style in sheet.styles.values():
        names.update(style.get("props", {}))
    for para in doc:
        names.update(para.get("props", {}))
        for _, _, span_props in para.get("spans", []):
            names.update(span_props)
    return sorted(names)


def expand_document(sheet: StyleSheet, doc: List[dict]) -> List[List[CharAttrs]]:
    """展开文档，返回与 doc 对齐的逐字符属性表（每个字符一个 {attr: (值, 来源)}）。"""
    sheet.resolve_all()
    universe = _property_universe(sheet, doc)
    # 默认值元组全局共享；样式/段落元组在同段内共享（值不可变、来源一致）。
    default_tuples = {
        prop: (value, "default") for prop, value in sheet.defaults.items()
    }

    result: List[List[CharAttrs]] = []
    for pidx, para in enumerate(doc):
        text: str = para["text"]
        style_name = para.get("style")
        if style_name is not None and style_name not in sheet.styles:
            sheet._missing(style_name, None)
        style_props = sheet.resolved(style_name)
        style_src = f"style:{style_name}"
        style_tuples = {prop: (value, style_src) for prop, value in style_props.items()}
        para_props: Dict[str, object] = para.get("props", {})
        para_tuples = {prop: (value, "paragraph") for prop, value in para_props.items()}

        # 预计算每个位置合并后的内联属性（后写 span 覆盖先写），避免逐字符扫描全部 span。
        length = len(text)
        inline_slots: List[Optional[Dict[str, object]]] = [None] * length
        for s, e, sp in para.get("spans", []):
            for ci in range(max(0, s), min(e, length)):
                if inline_slots[ci] is None:
                    inline_slots[ci] = dict(sp)
                else:
                    inline_slots[ci].update(sp)

        rows: List[CharAttrs] = []
        for ci in range(length):
            slot = inline_slots[ci]
            attrs: CharAttrs = {}
            if slot is None:
                for prop in universe:
                    if prop in para_tuples:
                        attrs[prop] = para_tuples[prop]
                    elif prop in style_tuples:
                        attrs[prop] = style_tuples[prop]
                    elif prop in default_tuples:
                        attrs[prop] = default_tuples[prop]
                    else:
                        raise ValueError(f"属性 '{prop}' 无任何来源且无默认值，拒绝静默丢弃")
            else:
                for prop in universe:
                    if prop in slot:
                        attrs[prop] = (slot[prop], "inline")
                    elif prop in para_tuples:
                        attrs[prop] = para_tuples[prop]
                    elif prop in style_tuples:
                        attrs[prop] = style_tuples[prop]
                    elif prop in default_tuples:
                        attrs[prop] = default_tuples[prop]
                    else:
                        raise ValueError(f"属性 '{prop}' 无任何来源且无默认值，拒绝静默丢弃")
            rows.append(attrs)
        result.append(rows)
    return result
