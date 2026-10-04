"""展开引擎：把 默认值/样式链/段落/内联 四层合并为逐字符最终属性。

优先级（高 -> 低）：
    内联(run.inline) > 段落直连(para_props) > 段落样式链(近 -> 远) > 显式默认值
任何层级都未给出的属性记为 (None, "none") 并产生 unresolved-property 诊断，
绝不静默丢弃。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .model import Document
from .resolver import analyze_styles, resolve_style_full, style_chain_layers


@dataclass
class Diagnostic:
    """一条展开诊断。kind 取值：
    cycle                    样式继承环（cycle 字段给出环上样式名）
    broken-style-ref         样式引用了不存在的父样式
    missing-paragraph-style  段落引用了不存在的样式
    unresolved-property      属性在任何层级都未定义，显式置为 null
    """

    kind: str
    message: str
    cycle: Optional[Tuple[str, ...]] = None
    prefix: Optional[Tuple[str, ...]] = None
    style: Optional[str] = None
    missing: Optional[str] = None
    paragraph: Optional[int] = None
    prop: Optional[str] = None


@dataclass
class ExpandedRun:
    run_index: int
    text: str
    vector: Tuple[Tuple[Any, str], ...]  # 与 ExpandedDoc.props 对齐的 (值, 来源)


@dataclass
class ExpandedParagraph:
    index: int
    style: Optional[str]
    runs: List[ExpandedRun] = field(default_factory=list)


@dataclass
class CharRow:
    """一个字符的最终属性（流式产生，不常驻内存）。"""

    para: int
    char_index: int
    run: int
    offset_in_run: int
    char: str
    values: Tuple[Any, ...]
    sources: Tuple[str, ...]


@dataclass
class ExpandedDoc:
    """展开结果：属性列全集 + 每段落/字符行的共享向量 + 诊断。"""

    props: Tuple[str, ...]
    paragraphs: List[ExpandedParagraph]
    diagnostics: List[Diagnostic]
    doc: Document = None  # 回指原文档，供 explain 使用

    def iter_rows(self) -> Iterator[CharRow]:
        for para in self.paragraphs:
            char_index = 0
            for run in para.runs:
                values = tuple(value for value, _ in run.vector)
                sources = tuple(source for _, source in run.vector)
                for offset, ch in enumerate(run.text):
                    yield CharRow(para.index, char_index, run.run_index,
                                  offset, ch, values, sources)
                    char_index += 1

    def explain(self, para_index: int, run_index: int) -> Dict[str, dict]:
        """返回 {属性: {value, source, candidates}}，candidates 按优先级从高到低，
        每层候选形如 {"layer", "value", "origin", "applied"}，便于核对冲突胜负。
        """
        para = self.doc.paragraphs[para_index]
        run = para.runs[run_index]
        style_name = para.style if para.style in self.doc.styles else None
        layers = style_chain_layers(self.doc.styles, style_name) if style_name else []
        result: Dict[str, dict] = {}
        for col, key in enumerate(self.props):
            candidates: List[dict] = []
            if key in run.inline:
                candidates.append({"layer": "inline", "value": run.inline[key],
                                   "origin": "inline", "applied": False})
            if key in para.para_props:
                candidates.append({"layer": "paragraph", "value": para.para_props[key],
                                   "origin": "paragraph", "applied": False})
            for origin, props in layers:
                if key in props:
                    candidates.append({"layer": "style", "value": props[key],
                                       "origin": origin, "applied": False})
            if key in self.doc.defaults:
                candidates.append({"layer": "default", "value": self.doc.defaults[key],
                                   "origin": "default", "applied": False})
            value, source = self.paragraphs[para_index].runs[run_index].vector[col]
            for cand in candidates:
                if cand["value"] == value and _source_of(cand) == source:
                    cand["applied"] = True
                    break
            result[key] = {"value": value, "source": source, "candidates": candidates}
        return result


def _source_of(cand: dict) -> str:
    if cand["layer"] == "style":
        return "style:" + cand["origin"]
    return cand["layer"]


def expand(doc: Document) -> ExpandedDoc:
    """展开整篇文档，返回 ExpandedDoc（含诊断列表）。"""
    diagnostics: List[Diagnostic] = []
    cycles, missing_edges = analyze_styles(doc.styles)
    for ring, prefix in cycles:
        diagnostics.append(Diagnostic(
            kind="cycle",
            message="检测到循环引用: " + " -> ".join(list(ring) + [ring[0]]),
            cycle=ring, prefix=prefix))
    for style_name, parent in missing_edges:
        diagnostics.append(Diagnostic(
            kind="broken-style-ref",
            message="样式 %r 引用了不存在的父样式 %r，缺失部分回退到显式默认值"
                    % (style_name, parent),
            style=style_name, missing=parent))

    style_cache: Dict[str, Dict[str, Tuple[Any, str]]] = {}
    universe: List[str] = list(doc.defaults.keys())
    seen = set(universe)

    def note(key: str) -> None:
        if key not in seen:
            seen.add(key)
            universe.append(key)

    # 第一遍：解析样式并收集属性全集
    per_para: List[Tuple[Any, Optional[str], Dict[str, Tuple[Any, str]]]] = []
    for para in doc.paragraphs:
        style_name = para.style if para.style in doc.styles else None
        style_full: Dict[str, Tuple[Any, str]] = {}
        if style_name is not None:
            style_full = resolve_style_full(doc.styles, style_name, style_cache)
            for key in style_full:
                note(key)
        for key in para.para_props:
            note(key)
        for run in para.runs:
            for key in run.inline:
                note(key)
        per_para.append((para, para.style, style_full))
    props = tuple(universe)

    def build_vector(style_full, para_props, inline):
        out = []
        for key in props:
            if key in inline:
                out.append((inline[key], "inline"))
            elif key in para_props:
                out.append((para_props[key], "paragraph"))
            elif key in style_full:
                value, origin = style_full[key]
                out.append((value, "style:" + origin))
            elif key in doc.defaults:
                out.append((doc.defaults[key], "default"))
            else:
                out.append((None, "none"))
        return tuple(out)

    vec_cache: Dict[Tuple, Tuple[Tuple[Any, str], ...]] = {}
    unresolved = set()
    expanded_paras: List[ExpandedParagraph] = []
    for para_idx, (para, style_name, style_full) in enumerate(per_para):
        if style_name is not None and style_name not in doc.styles:
            diagnostics.append(Diagnostic(
                kind="missing-paragraph-style",
                message="段落 %d 引用了不存在的样式 %r，按无样式处理"
                        % (para_idx, style_name),
                paragraph=para_idx, style=style_name))
        para_key = _cache_key(para.para_props)
        runs: List[ExpandedRun] = []
        for run_idx, run in enumerate(para.runs):
            inline_key = _cache_key(run.inline)
            cache_key = None
            if para_key is not None and inline_key is not None:
                cache_key = (style_name if style_name in doc.styles else None,
                             para_key, inline_key)
            vector = vec_cache.get(cache_key) if cache_key is not None else None
            if vector is None:
                vector = build_vector(style_full, para.para_props, run.inline)
                if cache_key is not None:
                    vec_cache[cache_key] = vector
            if run.text:  # 只统计真正产生字符的 run，与手工推算口径一致
                for col, (_, source) in enumerate(vector):
                    if source == "none":
                        unresolved.add((props[col], para_idx))
            runs.append(ExpandedRun(run_idx, run.text, vector))
        expanded_paras.append(ExpandedParagraph(para_idx, style_name, runs))

    for key, para_idx in sorted(unresolved):
        diagnostics.append(Diagnostic(
            kind="unresolved-property",
            message="属性 %r 在任何层级都未定义（段落 %d），显式置为 null"
                    % (key, para_idx),
            prop=key, paragraph=para_idx))

    return ExpandedDoc(props=props, paragraphs=expanded_paras,
                       diagnostics=diagnostics, doc=doc)


def _cache_key(mapping: Dict[str, Any]):
    """把属性映射变成可哈希的缓存键；含不可哈希值时返回 None（跳过缓存）。"""
    try:
        return tuple(sorted(mapping.items()))
    except TypeError:
        return None


def display_char(ch: str) -> str:
    if ch == "\n":
        return "\\n"
    if ch == "\t":
        return "\\t"
    if ch == "":
        return "∅"
    return ch


def write_char_table_tsv(expanded: ExpandedDoc, fileobj) -> None:
    """把逐字符属性表以 TSV 流式写出（每字符一行，每属性 值/来源 两列）。"""
    props = expanded.props
    header = ["para", "char_index", "run", "offset_in_run", "char", "codepoint"]
    for key in props:
        header.append(key)
        header.append(key + ":source")
    fileobj.write("\t".join(header) + "\n")
    for row in expanded.iter_rows():
        cells = [str(row.para), str(row.char_index), str(row.run),
                 str(row.offset_in_run), display_char(row.char),
                 "U+%04X" % ord(row.char)]
        for value, source in zip(row.values, row.sources):
            cells.append("" if value is None else str(value))
            cells.append(source)
        fileobj.write("\t".join(cells) + "\n")
