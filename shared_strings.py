"""共享字符串表 (Shared String Table, SST) 解析与单元格还原。

仅依赖标准库。数据模型与 xlsx 一致:
  - 重复文本只在共享表中存一份, 单元格仅保存整型索引;
  - 共享表条目 (<si>) 可以是纯文本, 也可以是带 <rPr> 格式标记的多个富文本片段 (<r>);
  - 单元格按显式坐标 (A1) 稀疏存储, 合并区域只按显式声明的 ref 展开。

存储复杂度: O(单元格数 + 共享表条目数), 与最大行/最大列无关, 不会按行列最大值平方增长。
"""
from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.sax.saxutils import escape

__all__ = [
    "Run",
    "RichText",
    "SharedStringTable",
    "Sheet",
    "SharedStringError",
    "MissingSharedStringTableError",
    "SharedStringIndexError",
    "MalformedCellError",
    "parse_coord",
    "format_coord",
    "parse_range",
    "build_sst",
    "dump_sst",
    "dump_sheet",
    "rich_inner_xml",
]


# ---------------------------------------------------------------------------
# 异常: 全部带表名与坐标, 拒绝用空白糊弄
# ---------------------------------------------------------------------------

class SharedStringError(Exception):
    """共享字符串相关错误的基类。"""


class MissingSharedStringTableError(SharedStringError):
    """单元格引用了共享表 (t="s"), 但解析时没有提供共享表。"""

    def __init__(self, sheet: str, coord: str):
        self.sheet = sheet
        self.coord = coord
        super().__init__(
            f"[{sheet}!{coord}] 单元格使用共享字符串索引 (t=\"s\"), "
            f"但未提供共享字符串表 (missing shared string table)"
        )


class SharedStringIndexError(SharedStringError):
    """共享字符串索引越界 (负索引或 >= 表长)。"""

    def __init__(self, sheet: str, coord: str, index: int, size: int):
        self.sheet = sheet
        self.coord = coord
        self.index = index
        self.size = size
        valid = f"[0, {size - 1}]" if size > 0 else "空表, 无合法索引"
        super().__init__(
            f"[{sheet}!{coord}] 共享字符串索引越界: index={index}, "
            f"表大小={size}, 合法索引区间={valid}"
        )


class MalformedCellError(SharedStringError):
    """单元格结构非法 (缺坐标、索引不是整数等)。"""


# ---------------------------------------------------------------------------
# 富文本: 片段顺序与格式标记都保留
# ---------------------------------------------------------------------------

# formats 是有序的 ((标签, (("属性","值"), ...)), ...), 例如
#   (("b", ()), ("sz", (("val", "11"),)), ("color", (("rgb", "FFFF0000"),)))
@dataclass(frozen=True)
class Run:
    text: str
    formats: tuple = ()


@dataclass(frozen=True)
class RichText:
    runs: tuple

    def __post_init__(self):
        if not self.runs:
            object.__setattr__(self, "runs", (Run(""),))

    @property
    def text(self) -> str:
        """按原顺序拼接所有富文本片段的纯文本。"""
        return "".join(run.text for run in self.runs)

    def format_tags(self) -> tuple:
        """每个片段的格式标签序列, 用于断言格式标记未丢失。"""
        return tuple(tuple(tag for tag, _attrs in run.formats) for run in self.runs)


# ---------------------------------------------------------------------------
# 坐标工具: A1 <-> (row, col), 1-based
# ---------------------------------------------------------------------------

_COORD_RE = re.compile(r"^\$?([A-Za-z]{1,})\$?([0-9]+)$")


def parse_coord(ref: str):
    match = _COORD_RE.match(ref.strip())
    if not match:
        raise ValueError(f"非法单元格坐标: {ref!r}")
    col = 0
    for ch in match.group(1).upper():
        col = col * 26 + (ord(ch) - ord("A") + 1)
    row = int(match.group(2))
    if row < 1 or col < 1:
        raise ValueError(f"非法单元格坐标: {ref!r}")
    return row, col


def format_coord(row: int, col: int) -> str:
    if row < 1 or col < 1:
        raise ValueError(f"行/列必须为正整数: row={row}, col={col}")
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return f"{letters}{row}"


def parse_range(ref: str):
    """'A1:B2' -> (r1, c1, r2, c2); 单格 'A1' -> (r,c,r,c)。"""
    parts = ref.split(":")
    if len(parts) == 1:
        r, c = parse_coord(parts[0])
        return r, c, r, c
    if len(parts) != 2:
        raise ValueError(f"非法区域引用: {ref!r}")
    r1, c1 = parse_coord(parts[0])
    r2, c2 = parse_coord(parts[1])
    if r2 < r1 or c2 < c1:
        raise ValueError(f"区域起点在终点之后: {ref!r}")
    return r1, c1, r2, c2


# ---------------------------------------------------------------------------
# XML 内部解析辅助
# ---------------------------------------------------------------------------

def _parse_rpr(rpr_elem):
    formats = []
    for child in rpr_elem:
        formats.append((child.tag, tuple(sorted(child.attrib.items()))))
    return tuple(formats)


def _parse_rich(parent_elem) -> RichText:
    """解析 <si> 或 <is>: 直接的 <t>, 以及 <r> 富文本片段, 严格按文档顺序。"""
    runs = []
    for child in parent_elem:
        if child.tag == "t":
            runs.append(Run(child.text or ""))
        elif child.tag == "r":
            rpr = child.find("rPr")
            formats = _parse_rpr(rpr) if rpr is not None else ()
            t_elem = child.find("t")
            runs.append(Run(t_elem.text if (t_elem is not None and t_elem.text) else "", formats))
        # rPh / phoneticPr 等拼音辅助元素忽略, 不影响可见文本
    return RichText(tuple(runs))


def _as_stream(source):
    if hasattr(source, "read"):
        return source
    if isinstance(source, bytes):
        return io.BytesIO(source)
    if isinstance(source, str):
        stripped = source.lstrip()
        if stripped[:1] == "<":
            return io.BytesIO(source.encode("utf-8"))
        if os.path.exists(source):
            return source
    raise TypeError(f"无法识别的输入类型: {type(source)!r}")


# ---------------------------------------------------------------------------
# 共享字符串表
# ---------------------------------------------------------------------------

class SharedStringTable:
    def __init__(self, entries=()):
        self._entries = tuple(entries)

    def __len__(self):
        return len(self._entries)

    def __contains__(self, index):
        return isinstance(index, int) and 0 <= index < len(self._entries)

    def lookup(self, index, sheet: str = "<unknown>", coord: str = "<unknown>") -> RichText:
        """按索引取条目; 越界/负索引一律拒绝并定位到表名与坐标。"""
        if not isinstance(index, int) or isinstance(index, bool):
            raise MalformedCellError(
                f"[{sheet}!{coord}] 共享字符串索引必须是整数, 得到 {index!r}"
            )
        if index < 0 or index >= len(self._entries):
            raise SharedStringIndexError(sheet, coord, index, len(self._entries))
        return self._entries[index]

    @classmethod
    def from_xml(cls, source) -> "SharedStringTable":
        entries = []
        for _event, elem in ET.iterparse(_as_stream(source), events=("end",)):
            if elem.tag == "si":
                entries.append(_parse_rich(elem))
                elem.clear()
            elif elem.tag == "sst":
                declared = elem.get("uniqueCount")
                if declared is not None and int(declared) != len(entries):
                    raise SharedStringError(
                        f"共享表头声明 uniqueCount={declared}, 实际条目数={len(entries)}"
                    )
        return cls(entries)


# ---------------------------------------------------------------------------
# 工作表: 稀疏单元格 + 显式合并区域
# ---------------------------------------------------------------------------

class Sheet:
    def __init__(self, name: str):
        self.name = name
        # {(row, col): RichText}; 与最大行/列无关的稀疏存储
        self.cells = {}
        # [(r1, c1, r2, c2), ...] 仅显式声明的合并区域
        self.merges = []

    def __len__(self):
        return len(self.cells)

    @property
    def bounds(self):
        if not self.cells:
            return 0, 0
        return max(r for r, _c in self.cells), max(c for _r, c in self.cells)

    def get(self, row: int, col: int):
        return self.cells.get((row, col))

    def cell_texts(self) -> dict:
        return {format_coord(r, c): v.text for (r, c), v in self.cells.items()}

    def expand_merges(self) -> "Sheet":
        """只展开显式声明的合并区域: 用左上角锚点值填入该 ref 内的空格。

        区域外的稀疏坐标一律不补; 绝不凭空生成整片行列。
        """
        for r1, c1, r2, c2 in self.merges:
            anchor = self.cells.get((r1, c1))
            if anchor is None:
                raise SharedStringError(
                    f"[{self.name}!{format_coord(r1, c1)}] 合并区域 "
                    f"{format_coord(r1, c1)}:{format_coord(r2, c2)} 缺少锚点单元格"
                )
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    self.cells.setdefault((r, c), anchor)
        return self

    @classmethod
    def from_xml(cls, source, sst=None, name: str = "sheet1") -> "Sheet":
        sheet = cls(name)
        context = ET.iterparse(_as_stream(source), events=("end",))
        for _event, elem in context:
            if elem.tag == "c":
                coord = elem.get("r")
                if not coord:
                    raise MalformedCellError(
                        f"[{sheet.name}] 单元格缺少显式坐标属性 r (只接受显式坐标)"
                    )
                try:
                    parse_coord(coord)
                except ValueError as exc:
                    raise MalformedCellError(
                        f"[{sheet.name}] 单元格坐标非法: {coord!r}"
                    ) from exc
                sheet.cells[parse_coord(coord)] = _parse_cell(elem, sst, name, coord)
                elem.clear()
            elif elem.tag == "mergeCell":
                ref = elem.get("ref")
                if not ref:
                    raise MalformedCellError(f"[{sheet.name}] mergeCell 缺少 ref 属性")
                sheet.merges.append(parse_range(ref))
                elem.clear()
            elif elem.tag == "row":
                elem.clear()
        return sheet


def _parse_cell(elem, sst, sheet_name, coord) -> RichText:
    cell_type = elem.get("t", "n")
    if cell_type == "s":
        raw = elem.findtext("v")
        if raw is None or raw.strip() == "":
            raise MalformedCellError(
                f"[{sheet_name}!{coord}] t=\"s\" 单元格缺少 <v> 索引值"
            )
        try:
            index = int(raw)
        except ValueError as exc:
            raise MalformedCellError(
                f"[{sheet_name}!{coord}] 共享字符串索引不是整数: {raw!r}"
            ) from exc
        if sst is None:
            raise MissingSharedStringTableError(sheet_name, coord)
        return sst.lookup(index, sheet_name, coord)
    if cell_type == "inlineStr":
        is_elem = elem.find("is")
        if is_elem is None:
            raise MalformedCellError(
                f"[{sheet_name}!{coord}] t=\"inlineStr\" 但缺少 <is> 元素"
            )
        return _parse_rich(is_elem)
    # 数字/布尔/公式字符串: 不涉及共享表, 原样保留文本
    value = elem.findtext("v")
    return RichText((Run(value if value is not None else ""),))


# ---------------------------------------------------------------------------
# 序列化 (对拍时压缩/未压缩两套数据用同一份代码写出, 保证可比)
# ---------------------------------------------------------------------------

_QUOT = {'"': "&quot;"}


def _t_xml(text: str) -> str:
    preserve = ' xml:space="preserve"' if text != text.strip() else ""
    return f"<t{preserve}>{escape(text)}</t>"


def _rpr_xml(formats) -> str:
    if not formats:
        return ""
    chunks = []
    for tag, attrs in formats:
        attr = "".join(f' {k}="{escape(v, _QUOT)}"' for k, v in attrs)
        chunks.append(f"<{tag}{attr}/>")
    return "<rPr>" + "".join(chunks) + "</rPr>"


def rich_inner_xml(rich: RichText) -> str:
    runs = rich.runs
    if len(runs) == 1 and not runs[0].formats:
        return _t_xml(runs[0].text)
    return "".join(
        f"<r>{_rpr_xml(run.formats)}{_t_xml(run.text)}</r>" for run in runs
    )


def build_sst(values):
    """根据 RichText 序列去重, 保持首次出现顺序, 返回 (entries, value->index 字典)。"""
    entries = []
    index = {}
    for value in values:
        if value not in index:
            index[value] = len(entries)
            entries.append(value)
    return entries, index


def dump_sst(entries) -> str:
    body = "".join(f"<si>{rich_inner_xml(e)}</si>" for e in entries)
    n = len(entries)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<sst count="{n}" uniqueCount="{n}">{body}</sst>'


def dump_sheet(cells, merges=(), sst_index=None) -> str:
    """cells: {(row, col): RichText}; sst_index 非 None 时写压缩格式 (t="s")。"""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<worksheet><sheetData>']
    by_row = {}
    for (r, c), value in cells.items():
        by_row.setdefault(r, []).append((c, value))
    for r in sorted(by_row):
        parts.append(f'<row r="{r}">')
        for c, value in sorted(by_row[r]):
            ref = format_coord(r, c)
            if sst_index is None:
                parts.append(
                    f'<c r="{ref}" t="inlineStr"><is>{rich_inner_xml(value)}</is></c>'
                )
            else:
                if value not in sst_index:
                    raise ValueError(f"单元格 {ref} 的值不在共享表中")
                parts.append(f'<c r="{ref}" t="s"><v>{sst_index[value]}</v></c>')
        parts.append("</row>")
    parts.append("</sheetData>")
    if merges:
        parts.append(f'<mergeCells count="{len(merges)}">')
        for r1, c1, r2, c2 in merges:
            parts.append(
                f'<mergeCell ref="{format_coord(r1, c1)}:{format_coord(r2, c2)}"/>'
            )
        parts.append("</mergeCells>")
    parts.append("</worksheet>")
    return "".join(parts)
