"""数据模型：样式、段落、字符行（run）与文档。"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Style:
    """具名段落样式，可通过 based_on 继承另一个样式。"""

    name: str
    props: Dict[str, Any] = field(default_factory=dict)
    based_on: Optional[str] = None


@dataclass
class Run:
    """一段具有相同内联属性的连续字符。"""

    text: str
    inline: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Paragraph:
    """一个段落：可选的段落样式 + 段落级直连属性 + 若干字符行。"""

    runs: List[Run] = field(default_factory=list)
    para_props: Dict[str, Any] = field(default_factory=dict)
    style: Optional[str] = None


@dataclass
class Document:
    """整篇文档：显式默认值 + 样式表 + 段落列表。"""

    defaults: Dict[str, Any] = field(default_factory=dict)
    styles: Dict[str, Style] = field(default_factory=dict)
    paragraphs: List[Paragraph] = field(default_factory=list)
