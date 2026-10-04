"""逐层手工推算的参考实现（对拍用，与 style_inherit 库相互独立）。

不做任何缓存/向量化：对每个字符、每个属性，直接按
    显式默认值 -> 样式链(远 -> 近逐层覆盖) -> 段落直连 -> 内联
的顺序手工覆盖，最后的胜出者与来源即为答案。
环的处理：沿 based_on 行走时每个样式只生效一次（行走顺序即优先级），
与库中“最远者优先”的环合并规则一致。
"""
from typing import Any, Dict, List, Tuple

from style_inherit.model import Document
from style_inherit.resolver import analyze_styles, normalize_based_on


def manual_expand(doc: Document) -> Tuple[List[dict], List[dict]]:
    """返回 (rows, diagnostics)。rows 中每个字符一个 dict。"""
    diagnostics: List[dict] = []
    cycles, missing_edges = analyze_styles(doc.styles)
    for ring, prefix in cycles:
        diagnostics.append({
            "kind": "cycle", "cycle": list(ring), "prefix": list(prefix),
            "message": "检测到循环引用: " + " -> ".join(list(ring) + [ring[0]])})
    for style_name, parent in missing_edges:
        diagnostics.append({
            "kind": "broken-style-ref", "style": style_name, "missing": parent,
            "message": "样式 %r 引用了不存在的父样式 %r，缺失部分回退到显式默认值"
                       % (style_name, parent)})

    universe = set(doc.defaults)
    for style in doc.styles.values():
        universe |= set(style.props)
    for para in doc.paragraphs:
        universe |= set(para.para_props)
        for run in para.runs:
            universe |= set(run.inline)

    rows: List[dict] = []
    unresolved = set()
    for para_idx, para in enumerate(doc.paragraphs):
        style_name = para.style
        if style_name is not None and style_name not in doc.styles:
            diagnostics.append({
                "kind": "missing-paragraph-style", "paragraph": para_idx,
                "style": style_name,
                "message": "段落 %d 引用了不存在的样式 %r，按无样式处理"
                           % (para_idx, style_name)})
            style_name = None
        # 手工沿父链逐层合并（远的祖先先写，近的样式覆盖）
        chain = []
        seen_on_chain = set()
        cur = style_name
        while cur is not None and cur in doc.styles and cur not in seen_on_chain:
            seen_on_chain.add(cur)
            chain.append(doc.styles[cur])
            cur = normalize_based_on(doc.styles[cur].based_on)
        style_props: Dict[str, Any] = {}
        style_origin: Dict[str, str] = {}
        for style in reversed(chain):
            for key, value in style.props.items():
                style_props[key] = value
                style_origin[key] = style.name
        char_index = 0
        for run_idx, run in enumerate(para.runs):
            for offset, ch in enumerate(run.text):
                final = {}
                for key in universe:  # 逐层手工覆盖，高优先级后写
                    if key in run.inline:
                        final[key] = (run.inline[key], "inline")
                    elif key in para.para_props:
                        final[key] = (para.para_props[key], "paragraph")
                    elif key in style_props:
                        final[key] = (style_props[key],
                                      "style:" + style_origin[key])
                    elif key in doc.defaults:
                        final[key] = (doc.defaults[key], "default")
                    else:
                        final[key] = (None, "none")
                        unresolved.add((key, para_idx))
                rows.append({"para": para_idx, "char_index": char_index,
                             "run": run_idx, "offset_in_run": offset,
                             "char": ch, "props": final})
                char_index += 1
    for key, para_idx in sorted(unresolved):
        diagnostics.append({
            "kind": "unresolved-property", "prop": key, "paragraph": para_idx,
            "message": "属性 %r 在任何层级都未定义（段落 %d），显式置为 null"
                       % (key, para_idx)})
    return rows, diagnostics


def diag_signature(diag) -> tuple:
    """把库 Diagnostic 或手工 dict 统一成可比较的元组。"""
    if isinstance(diag, dict):
        return (diag.get("kind"),
                tuple(diag.get("cycle") or ()) or None,
                tuple(diag.get("prefix") or ()) or None,
                diag.get("style"), diag.get("missing"),
                diag.get("paragraph"), diag.get("prop"))
    return (diag.kind, diag.cycle or None, diag.prefix or None, diag.style,
            diag.missing, diag.paragraph, diag.prop)


def diff_expand(doc: Document) -> List[str]:
    """对拍：库展开结果 vs 手工推算结果，返回不一致列表（空 = 完全一致）。"""
    from style_inherit import expand  # 延迟导入，保持模块独立

    expanded = expand(doc)
    manual_rows, manual_diags = manual_expand(doc)
    problems: List[str] = []

    lib_rows = list(expanded.iter_rows())
    if len(lib_rows) != len(manual_rows):
        problems.append("行数不一致: 库=%d 手工=%d"
                        % (len(lib_rows), len(manual_rows)))
        return problems
    for pos, (lib_row, man_row) in enumerate(zip(lib_rows, manual_rows)):
        where = "行%d(p%d,c%d)" % (pos, man_row["para"], man_row["char_index"])
        if (lib_row.para, lib_row.char_index, lib_row.run,
                lib_row.offset_in_run, lib_row.char) != (
                man_row["para"], man_row["char_index"], man_row["run"],
                man_row["offset_in_run"], man_row["char"]):
            problems.append("%s 字符定位不一致" % where)
            continue
        for col, key in enumerate(expanded.props):
            lib_pair = (lib_row.values[col], lib_row.sources[col])
            man_pair = man_row["props"][key]
            if lib_pair != man_pair:
                problems.append("%s 属性 %r 不一致: 库=%r 手工=%r"
                                % (where, key, lib_pair, man_pair))
    lib_sigs = sorted(map(str, map(diag_signature, expanded.diagnostics)))
    man_sigs = sorted(map(str, map(diag_signature, manual_diags)))
    if lib_sigs != man_sigs:
        problems.append("诊断不一致: 库=%s 手工=%s" % (lib_sigs, man_sigs))
    return problems
